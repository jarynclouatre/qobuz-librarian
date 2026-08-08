"""Library migration - reorganise an existing collection into this tool's layout.

The job is to take an arbitrary, possibly-untagged music folder and produce the
two-level ``<Artist>/<Album> (<Year>)/`` tree the scanner expects, matching the
exact folder shape the downloader itself writes (see ``docker/beets-default.yaml``:
``$albumartist/$album ($year)/%if{$multidisc,Disc $disc/}$track - $title``).

Three rules drive every decision here:

* **Copy unless explicitly moving.** The default builds the organised library
  as a copy at a separate destination; the source is read but not altered or
  deleted. In-place mode is a separate, explicit opt-in and even then only
  relocates a file once a verified copy exists.
* **Decide, then act.** ``build_plan`` produces the entire set of
  source→destination decisions and seals the reviewed filesystem identities.
  The preview renders that plan; ``execute_plan`` carries out exactly that plan.
  The preview is the truth, not an estimate.
* **When unsure, leave it.** A file whose tags can't place it, an ambiguous
  fingerprint, or a destination collision is left untouched and reported - never
  guessed into the wrong folder.

Tags are the primary source of truth. Fingerprinting (AcoustID) is an opt-in
second stage that only runs against the files tags couldn't place, so a
mostly-tagged library finishes fast and offline.
"""
import base64
import csv
import ctypes
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional

from qobuz_librarian import config
from qobuz_librarian.library.tags import (
    VA_NORMALIZED,
    beets_sanitize,
    normalize,
    strip_year_decoration,
)
from qobuz_librarian.ui_cli.logging import vlog

log = logging.getLogger("qobuz_librarian")

# beets' chroma plugin ships this public application key and uses it for every
# AcoustID *lookup*; a personal key is only ever needed to *submit* fingerprints
# back. Identification is therefore keyless - reuse the same key here so the
# migration matches what `beet -c beets-chroma.yaml import` would resolve.
ACOUSTID_API_KEY = "1vOwZtEn"

# AcoustID asks callers to stay under ~3 lookups/second on a shared key.
ACOUSTID_RATE_DELAY = 0.34
# A fingerprint match below this confidence, or one that disagrees with another
# high-scoring match, is treated as "couldn't identify" rather than guessed.
ACOUSTID_MIN_SCORE = 0.9

PLACE = "place"
UNPLACEABLE = "unplaceable"
COLLISION = "collision"

COPIED = "copied"
SKIPPED = "skipped"
FAILED = "failed"


@dataclass
class PlanEntry:
    source: Path
    status: str                       # PLACE | UNPLACEABLE | COLLISION
    dest_rel: Optional[Path] = None   # relative to the destination root
    source_of_truth: str = ""         # "tags" | "acoustid"
    reason: str = ""
    meta: Optional[dict] = None
    # Captured while the preview is built.  Execution refuses an entry without
    # this proof instead of binding whatever later occupies ``source``.
    source_receipt: Optional[dict] = None
    # Present only for a preview-time destination collision.  Resume handling
    # must prove that the same destination is still there before using it.
    destination_receipt: Optional[dict] = None
    # Exact existing parents plus the parent suffix that was absent at preview.
    # Execution may create that suffix, but must refuse anything else that
    # appears there between review and publication.
    destination_path_receipt: Optional[dict] = None


@dataclass
class MigrationPlan:
    dest_root: Path
    entries: list = field(default_factory=list)
    source_root: Optional[Path] = None
    source_root_receipt: Optional[dict] = None
    dest_root_receipt: Optional[dict] = None
    companion_receipts: list = field(default_factory=list)
    destination_name_semantics: Optional[dict] = None

    @property
    def placed(self) -> list:
        return [e for e in self.entries if e.status == PLACE]

    @property
    def unplaceable(self) -> list:
        return [e for e in self.entries if e.status == UNPLACEABLE]

    @property
    def collisions(self) -> list:
        return [e for e in self.entries if e.status == COLLISION]

    def summary(self) -> dict:
        return {
            "total": len(self.entries),
            "place": len(self.placed),
            "unplaceable": len(self.unplaceable),
            "collision": len(self.collisions),
        }


@dataclass
class ExecResult:
    copied: int = 0
    skipped: int = 0
    failed: int = 0
    # In-place moves that reached the destination but whose source couldn't be
    # deleted, so the original still occupies space - counted apart from clean
    # moves so the summary doesn't claim them as fully relocated.
    lingered: int = 0
    # Non-audio companion files (cover art, .cue/.log/.lrc/…) carried alongside
    # the audio into the destination so a migrated album keeps its artwork.
    companions: int = 0
    # Empty source directories removed through their preview-sealed identities
    # after all exact in-place file retirements have completed.
    pruned: int = 0
    cancelled: bool = False
    # One (source, dest_rel, status, reason) per attempted file - the record of
    # what actually happened, surfaced to the user and written to the results
    # manifest. status is COPIED | SKIPPED | FAILED.
    outcomes: list = field(default_factory=list)
    # Companion work has its own count and must not distort the track summary,
    # but every attempt still belongs in the durable results evidence.
    companion_outcomes: list = field(default_factory=list)
    # Exact private locations retained after an uncertain namespace outcome.
    # Each record is deliberately serializable for CLI/Web persistence.
    recoveries: list = field(default_factory=list)
    # Teardown happens after file outcomes may already be committed. Keep any
    # uncertainty in the same durable result instead of letting a later close
    # failure erase the evidence for those files.
    cleanup_failures: list = field(default_factory=list)

    @property
    def failures(self) -> list:
        return [(src, reason) for src, _, status, reason in self.outcomes
                if status == FAILED]


class MigrationExecutionAbort(BaseException):
    """Carry a reconciled partial result to its durable publication boundary."""

    def __init__(self, result: ExecResult, cause: BaseException):
        super().__init__(str(cause) or type(cause).__name__)
        self.result = result
        self.cause = cause

    def note_cleanup_failure(self, boundary: str, failure: BaseException) -> None:
        record = _record_cleanup_failure(self.result, boundary, failure)
        BaseException.add_note(
            self.cause,
            "Migration cleanup was uncertain at "
            f"{record['boundary']}: {record['error']}. "
            "Inspect the partial results report before another migration."
        )

    def reraise(self, publication_error: BaseException | None = None):
        """Restore the original failure after the partial-result write."""
        traceback = self.cause.__traceback__
        if publication_error is not None:
            BaseException.add_note(
                self.cause,
                "The partial migration result also could not be published: "
                f"{publication_error}"
            )
            raise self.cause.with_traceback(traceback) from publication_error
        raise self.cause.with_traceback(traceback)


def _cleanup_failure_record(boundary: str, failure: BaseException) -> dict:
    message = str(failure)
    error = type(failure).__name__
    if message:
        error = f"{error}: {message}"
    return {
        "boundary": str(boundary),
        "error": error,
    }


def _record_cleanup_failure(result: ExecResult, boundary: str,
                            failure: BaseException) -> dict:
    record = _cleanup_failure_record(boundary, failure)
    if record not in result.cleanup_failures:
        result.cleanup_failures.append(record)
    result.cancelled = True
    return record


def _raise_cleanup_abort(result: ExecResult, failures, *, primary=None) -> None:
    """Publish every cleanup failure without replacing the primary failure."""
    if not failures:
        return
    abort = MigrationExecutionAbort(
        result,
        primary if primary is not None else failures[0][1],
    )
    for boundary, failure in failures:
        abort.note_cleanup_failure(boundary, failure)
    raise abort from abort.cause


def _capture_cleanup(failures, boundary: str, cleanup) -> None:
    try:
        cleanup()
    except BaseException as exc:
        failures.append((boundary, exc))


def _migration_abort_in_chain(failure: BaseException | None):
    pending = [failure] if failure is not None else []
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, MigrationExecutionAbort):
            return current
        for linked in (current.__cause__, current.__context__):
            if isinstance(linked, BaseException):
                pending.append(linked)
    return None


# ── Tag extraction ──────────────────────────────────────────────────────────

def _first(tags: Mapping, *keys) -> str:
    """First non-empty value across the given keys, case-insensitively.

    Tag backends return either scalars or single-element lists; both collapse
    to a stripped string here.
    """
    lower = {str(k).lower(): v for k, v in tags.items()}
    for k in keys:
        v = lower.get(k.lower())
        if isinstance(v, (list, tuple)):
            v = v[0] if v else ""
        v = str(v).strip() if v is not None else ""
        if v:
            return v
    return ""


def _num_and_total(s: str):
    """Parse a "3", "03", or "3/12" tag into (number, total). Either may be 0."""
    if not s:
        return 0, 0
    m = re.match(r"^\s*(\d+)\s*(?:/\s*(\d+))?", str(s))
    if not m:
        return 0, 0
    return int(m.group(1)), int(m.group(2) or 0)


def _parse_year(s: str) -> int:
    m = re.search(r"(\d{4})", str(s or ""))
    if not m:
        return 0
    y = int(m.group(1))
    return y if 1000 <= y <= 9999 else 0


def _is_compilation(tags: Mapping, albumartist: str) -> bool:
    flag = _first(tags, "compilation", "cpil", "tcmp").lower()
    if flag in ("1", "yes", "true"):
        return True
    return normalize(albumartist) in VA_NORMALIZED


def normalize_tags(tags: Mapping, stem: str, ext: str) -> dict:
    """Map a raw tag mapping to the fields placement needs.

    Pure: takes an already-read mapping (so it's testable without a real audio
    file). ``stem`` is the source filename minus extension, used as the title
    fallback when no title tag is present.
    """
    albumartist = _first(tags, "albumartist", "album artist", "artist")
    track, _ = _num_and_total(_first(tags, "tracknumber", "track"))
    disc, disc_total_inline = _num_and_total(_first(tags, "discnumber", "disc"))
    disc_total, _ = _num_and_total(_first(tags, "disctotal", "totaldiscs"))
    return {
        "albumartist": albumartist,
        "album": _first(tags, "album"),
        "title": _first(tags, "title") or stem,
        "track": track,
        "disc": disc or 1,
        "disctotal": disc_total or disc_total_inline or 0,
        "year": _parse_year(_first(tags, "originaldate", "date", "year")),
        "compilation": _is_compilation(tags, albumartist),
        "ext": ext.lower(),
    }


def extract_metadata(path: Path, *, descriptor=None) -> Optional[dict]:
    """Read tags from any supported audio file via mutagen; None if unreadable.

    ``easy=True`` gives MP3/MP4 the same friendly key names FLAC/OGG already use,
    so ``normalize_tags`` sees one vocabulary regardless of container.
    """
    try:
        import mutagen
    except ImportError:
        return None
    try:
        if descriptor is None:
            f = mutagen.File(str(path), easy=True)
        else:
            from mutagen._util import FileThing
            with os.fdopen(os.dup(descriptor), "rb") as fileobj:
                filething = FileThing(
                    fileobj, os.fspath(path), os.fspath(path))
                f = mutagen.File(filething, easy=True)
    except Exception:
        return None
    tags = dict(f.tags) if (f is not None and f.tags) else {}
    return normalize_tags(tags, path.stem, path.suffix)


# ── Placement ────────────────────────────────────────────────────────────────

def is_placeable(meta: Optional[dict]) -> bool:
    """A file can be placed from metadata once it has an album artist and album.

    The title falls back to the source filename, so it isn't required; without an
    artist or album there's no folder to build, so the file is left alone."""
    if not meta:
        return False
    return bool(meta.get("albumartist") and meta.get("album"))


_NAME_MAX = 255  # bytes - the per-component limit on ext4/APFS/NTFS and POSIX


def _truncate_component(name: str, *, ext: str = "", limit: int = _NAME_MAX) -> str:
    """Cap a single path component at the filesystem's per-name BYTE limit (beets
    caps at the same 255 by default). A tag long enough to exceed NAME_MAX would
    otherwise raise ENAMETOOLONG from the first Path.exists()/copy and abort the
    whole migration before any plan is built. Truncates on a UTF-8 char boundary
    and reserves room for the extension."""
    ext_b = ext.encode("utf-8", "surrogateescape")
    budget = max(1, limit - len(ext_b))
    b = name.encode("utf-8", "surrogateescape")
    if len(b) <= budget:
        return name + ext
    # Drop a partial trailing multi-byte char rather than splitting it.
    return b[:budget].decode("utf-8", "ignore") + ext


def album_components(meta: dict) -> tuple:
    """(artist_folder, album_folder) for an album, sanitized to match beets.

    Compilations route under ``Various Artists``; the year is appended only when
    known, so ``Album ()`` never appears."""
    if meta.get("compilation"):
        artist = "Various Artists"
    else:
        artist = meta["albumartist"]
    year = meta.get("year") or 0
    album = meta["album"]
    if year:
        # Strip any year the album tag already carries so we don't end up with
        # "Album (2010) (2010)".
        album_folder = f"{strip_year_decoration(album)} ({year})"
    else:
        album_folder = album
    return (_truncate_component(beets_sanitize(artist)),
            _truncate_component(beets_sanitize(album_folder)))


def track_filename(meta: dict) -> str:
    track = meta.get("track") or 0
    title = meta.get("title") or ""
    name = f"{track:02d} - {title}" if track > 0 else title
    return _truncate_component(beets_sanitize(name), ext=meta.get("ext", ""))


def _album_key(meta: dict) -> tuple:
    return album_components(meta)


# ── Sealed filesystem evidence ──────────────────────────────────────────────

_RENAME_NOREPLACE = 1
_AT_EMPTY_PATH = 0x1000
_AT_SYMLINK_NOFOLLOW = 0x100
_STATX_TYPE = 0x0001
_STATX_MODE = 0x0002
_STATX_INO = 0x0100
_STATX_BASIC_STATS = 0x07FF
_STATX_BTIME = 0x0800
_STATX_MNT_ID = 0x1000
_STATX_FUNCTION = None


class _StatxTimestamp(ctypes.Structure):
    _fields_ = [
        ("tv_sec", ctypes.c_int64),
        ("tv_nsec", ctypes.c_uint32),
        ("reserved", ctypes.c_int32),
    ]


class _Statx(ctypes.Structure):
    _fields_ = [
        ("mask", ctypes.c_uint32),
        ("blksize", ctypes.c_uint32),
        ("attributes", ctypes.c_uint64),
        ("nlink", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("mode", ctypes.c_uint16),
        ("spare0", ctypes.c_uint16),
        ("ino", ctypes.c_uint64),
        ("size", ctypes.c_uint64),
        ("blocks", ctypes.c_uint64),
        ("attributes_mask", ctypes.c_uint64),
        ("atime", _StatxTimestamp),
        ("btime", _StatxTimestamp),
        ("ctime", _StatxTimestamp),
        ("mtime", _StatxTimestamp),
        ("rdev_major", ctypes.c_uint32),
        ("rdev_minor", ctypes.c_uint32),
        ("dev_major", ctypes.c_uint32),
        ("dev_minor", ctypes.c_uint32),
        ("mnt_id", ctypes.c_uint64),
        ("dio_mem_align", ctypes.c_uint32),
        ("dio_offset_align", ctypes.c_uint32),
        ("spare3", ctypes.c_uint64 * 12),
    ]


def _statx_function():
    global _STATX_FUNCTION
    try:
        if _STATX_FUNCTION is None:
            statx = ctypes.CDLL(None, use_errno=True).statx
            statx.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_uint,
                ctypes.POINTER(_Statx),
            )
            statx.restype = ctypes.c_int
            _STATX_FUNCTION = statx
        return _STATX_FUNCTION
    except AttributeError as exc:
        raise OSError(
            errno.ENOSYS, "statx filesystem proof is unavailable"
        ) from exc


def _statx_probe(descriptor, path, flags, mask) -> _Statx:
    value = _Statx()
    ctypes.set_errno(0)
    if _statx_function()(
            int(descriptor), os.fsencode(path), flags, mask,
            ctypes.byref(value)):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return value


def _statx_directory_identity(descriptor, path, flags) -> list:
    value = _statx_probe(
        descriptor, path, flags,
        _STATX_BASIC_STATS | _STATX_BTIME | _STATX_MNT_ID)
    # Only the fields the identity reads must be present; a filesystem that
    # omits unused stats such as atime (ZFS atime=off) can still be proved.
    required = _STATX_TYPE | _STATX_MODE | _STATX_INO | _STATX_BTIME | _STATX_MNT_ID
    if value.mask & required != required or value.btime.tv_nsec >= 1_000_000_000:
        raise OSError("filesystem cannot prove directory incarnation safely")
    device = os.makedev(value.dev_major, value.dev_minor)
    identity = [
        int(stat.S_IFMT(value.mode)),
        int(device),
        int(value.ino),
        int(value.mode),
        int(value.btime.tv_sec),
        int(value.btime.tv_nsec),
        int(value.mnt_id),
    ]
    if not stat.S_ISDIR(value.mode):
        raise OSError("statx directory proof is not a directory")
    return identity


def _descriptor_mount_id(descriptor) -> int:
    """Return the held entry's Linux mount ID, refusing weak fallbacks."""
    value = _statx_probe(
        descriptor, b"", _AT_EMPTY_PATH | _AT_SYMLINK_NOFOLLOW, _STATX_MNT_ID)
    if not value.mask & _STATX_MNT_ID or value.mnt_id <= 0:
        raise OSError("filesystem cannot prove mount identity safely")
    return int(value.mnt_id)


def _directory_identity(descriptor) -> list:
    """Immutable, serializable directory-incarnation proof.

    Device/inode alone is recyclable. Linux ``statx`` birth time identifies the
    inode incarnation while mount ID distinguishes separately mounted views.
    A filesystem or kernel that cannot supply both must be rescanned instead of
    falling back to mutable ctime or accepting a legacy three-field receipt.
    """
    identity = _statx_directory_identity(
        descriptor,
        b"",
        _AT_EMPTY_PATH | _AT_SYMLINK_NOFOLLOW,
    )
    current = os.fstat(descriptor)
    if (
        int(current.st_dev) != identity[1]
        or int(current.st_ino) != identity[2]
        or int(current.st_mode) != identity[3]
    ):
        raise OSError("statx directory proof disagrees with the held directory")
    return identity


def _named_directory_identity(parent_fd, name) -> list:
    return _statx_directory_identity(
        parent_fd,
        name,
        _AT_SYMLINK_NOFOLLOW,
    )


def _receipt_directory_identity(value) -> Optional[list]:
    if (
        not isinstance(value, list)
        or any(type(part) is not int for part in value)
    ):
        return None
    identity = list(value)
    if (
        len(identity) != 7
        or identity[0] != stat.S_IFDIR
        or identity[1] < 0
        or identity[2] <= 0
        or not stat.S_ISDIR(identity[3])
        or identity[5] < 0
        or identity[5] >= 1_000_000_000
        or identity[6] <= 0
    ):
        return None
    return identity


def _entry_identity(value) -> list:
    return [
        int(stat.S_IFMT(value.st_mode)),
        int(value.st_dev),
        int(value.st_ino),
    ]


def _same_entry(left, right) -> bool:
    return _entry_identity(left) == _entry_identity(right)


def _receipt_identity(value) -> Optional[list]:
    if (
        not isinstance(value, list)
        or any(type(part) is not int for part in value)
    ):
        return None
    identity = list(value)
    return identity if len(identity) == 3 else None


def _safe_component(value) -> bool:
    separators = {os.sep}
    if os.altsep:
        separators.add(os.altsep)
    return (
        isinstance(value, str)
        and value not in ("", ".", "..")
        and "\x00" not in value
        and not any(separator in value for separator in separators)
    )


def _open_directory(path, *, dir_fd=None) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError("safe no-follow directory access is unavailable")
    return os.open(
        os.fspath(path),
        os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0),
        dir_fd=dir_fd,
    )


def _open_regular_file(name, *, dir_fd) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("safe no-follow file access is unavailable")
    flags = (
        os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    noatime = getattr(os, "O_NOATIME", 0)
    try:
        descriptor = os.open(
            os.fspath(name), flags | noatime, dir_fd=dir_fd)
    except OSError as exc:
        if not noatime or exc.errno not in (errno.EACCES, errno.EPERM):
            raise
        descriptor = os.open(os.fspath(name), flags, dir_fd=dir_fd)
    try:
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode):
            raise OSError("migration source is not a regular file")
        if not _named_entry_matches(dir_fd, name, descriptor):
            raise OSError("migration source changed while it was opened")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_entry_handle(name, *, dir_fd) -> int:
    path_only = getattr(os, "O_PATH", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if path_only is None or nofollow is None:
        raise OSError("safe no-follow entry access is unavailable")
    descriptor = os.open(
        os.fspath(name),
        path_only | nofollow | getattr(os, "O_CLOEXEC", 0),
        dir_fd=dir_fd,
    )
    if not _named_entry_matches(dir_fd, name, descriptor):
        os.close(descriptor)
        raise OSError("migration entry changed while it was opened")
    return descriptor


def _descriptor_path(descriptor) -> Path:
    """Current concrete Linux path for a held, still-linked descriptor."""
    try:
        value = os.readlink(f"/proc/self/fd/{int(descriptor)}")
    except (OSError, TypeError, ValueError) as exc:
        raise OSError("held recovery path could not be resolved") from exc
    if value.endswith(" (deleted)") or not Path(value).is_absolute():
        raise OSError("held recovery directory has no stable public path")
    return Path(value)


def _named_entry_matches(parent_fd, name, descriptor) -> bool:
    try:
        return _same_entry(
            os.fstat(descriptor),
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False),
        )
    except (OSError, TypeError, ValueError):
        return False


def _named_entry_missing(parent_fd, name) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return False


def _rename_noreplace_at(source_fd, source_name, destination_fd,
                         destination_name) -> None:
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable") from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    ctypes.set_errno(0)
    if renameat2(
            int(source_fd), os.fsencode(source_name),
            int(destination_fd), os.fsencode(destination_name),
            _RENAME_NOREPLACE):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), os.fspath(destination_name))


class _MigrationEntryPreserved(OSError):
    """Report an exact private entry that could not be restored safely."""

    def __init__(self, location, message):
        self.location = os.fspath(location)
        super().__init__(errno.EBUSY, message)


def _preserved_entry_error(parent_fd, name, message):
    return _MigrationEntryPreserved(
        _descriptor_path(parent_fd) / name,
        message,
    )


def _rename_exact_noreplace_at(source_fd, source_name, destination_fd,
                               destination_name, expected_fd):
    """Move one held inode, restoring a late source replacement."""
    if (
        not _named_entry_matches(source_fd, source_name, expected_fd)
        or not _named_entry_missing(destination_fd, destination_name)
    ):
        raise OSError("exact no-replace rename precondition changed")
    deferred = None
    try:
        _rename_noreplace_at(
            source_fd, source_name, destination_fd, destination_name)
    except BaseException as exc:
        deferred = exc
    if (
        _named_entry_missing(source_fd, source_name)
        and _named_entry_matches(
            destination_fd, destination_name, expected_fd)
    ):
        sync_error = None
        try:
            _fsync_directories(source_fd, destination_fd)
        except BaseException as exc:
            sync_error = exc
        return deferred or sync_error

    # renameat2 operates on a name, not the descriptor checked above.  If the
    # public name was swapped at that boundary, put the exact unexpected entry
    # back without overwriting anything instead of absorbing it into cleanup.
    if _named_entry_missing(source_fd, source_name):
        unexpected_fd = None
        try:
            unexpected_fd = _open_entry_handle(
                destination_name, dir_fd=destination_fd)
            rollback_error = None
            try:
                _rename_noreplace_at(
                    destination_fd,
                    destination_name,
                    source_fd,
                    source_name,
                )
            except BaseException as exc:
                rollback_error = exc
            if (
                _named_entry_missing(destination_fd, destination_name)
                and _named_entry_matches(
                    source_fd, source_name, unexpected_fd)
            ):
                try:
                    _fsync_directories(source_fd, destination_fd)
                except BaseException as exc:
                    rollback_error = rollback_error or exc
                raise OSError(
                    "publication was replaced during cleanup; the "
                    "replacement was restored"
                ) from (deferred or rollback_error)
            if _named_entry_matches(
                    destination_fd, destination_name, unexpected_fd):
                raise _preserved_entry_error(
                    destination_fd,
                    destination_name,
                    "a second arrival blocked restoration of the first "
                    "replacement",
                ) from (deferred or rollback_error)
        except FileNotFoundError:
            pass
        finally:
            if unexpected_fd is not None:
                os.close(unexpected_fd)
    if (
        deferred is not None
        and _named_entry_matches(source_fd, source_name, expected_fd)
        and _named_entry_missing(destination_fd, destination_name)
    ):
        raise deferred
    raise OSError("exact no-replace rename outcome could not be reconciled") \
        from deferred


def _fsync_directories(*descriptors) -> None:
    seen = set()
    for descriptor in descriptors:
        if descriptor in seen:
            continue
        seen.add(descriptor)
        os.fsync(descriptor)


def _digest_fd(descriptor, _chunk=1 << 20) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        data = os.pread(descriptor, _chunk, offset)
        if not data:
            return digest.hexdigest()
        digest.update(data)
        offset += len(data)


def _xattr_snapshot(descriptor) -> list:
    """Serializable exact snapshot of all visible extended attributes.

    POSIX ACLs are represented by ``system.posix_acl_*`` xattrs on Linux, so
    this one proof covers both ordinary xattrs and the filesystem's ACL form.
    """
    try:
        names = sorted(os.fsencode(name) for name in os.listxattr(descriptor))
        return [
            [
                base64.b64encode(name).decode("ascii"),
                base64.b64encode(
                    os.getxattr(descriptor, os.fsdecode(name))).decode("ascii"),
            ]
            for name in names
        ]
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise OSError("migration file metadata could not be sealed") from exc


def _decode_xattr_snapshot(value) -> list:
    if not isinstance(value, list):
        raise OSError("migration xattr receipt is malformed")
    decoded = []
    seen = set()
    try:
        for item in value:
            if not isinstance(item, list) or len(item) != 2:
                raise ValueError
            name = base64.b64decode(item[0], validate=True)
            payload = base64.b64decode(item[1], validate=True)
            if not name or b"\x00" in name or name in seen:
                raise ValueError
            seen.add(name)
            decoded.append((name, payload))
    except (TypeError, ValueError) as exc:
        raise OSError("migration xattr receipt is malformed") from exc
    return decoded


def _file_receipt(descriptor, digest=None) -> dict:
    value = os.fstat(descriptor)
    if not stat.S_ISREG(value.st_mode):
        raise OSError("migration source is not a regular file")
    receipt = {
        "identity": _entry_identity(value),
        "mode": int(value.st_mode),
        "uid": int(value.st_uid),
        "gid": int(value.st_gid),
        "size": int(value.st_size),
        "atime_ns": int(value.st_atime_ns),
        "mtime_ns": int(value.st_mtime_ns),
        "changed_ns": int(value.st_ctime_ns),
        "xattrs": _xattr_snapshot(descriptor),
    }
    if digest is not None:
        receipt["sha256"] = str(digest)
    return receipt


def _file_receipt_matches(descriptor, receipt, *, digest=None,
                          allow_changed_time=False) -> bool:
    try:
        value = os.fstat(descriptor)
        expected = _receipt_identity(receipt.get("identity"))
        if (
            expected is None
            or not stat.S_ISREG(value.st_mode)
            or _entry_identity(value) != expected
            or int(value.st_mode) != int(receipt["mode"])
            or int(value.st_uid) != int(receipt["uid"])
            or int(value.st_gid) != int(receipt["gid"])
            or int(value.st_size) != int(receipt["size"])
            or int(value.st_atime_ns) != int(receipt["atime_ns"])
            or int(value.st_mtime_ns) != int(receipt["mtime_ns"])
            or (not allow_changed_time
                and int(value.st_ctime_ns) != int(receipt["changed_ns"]))
            or _xattr_snapshot(descriptor) != receipt["xattrs"]
        ):
            return False
        return (digest if digest is not None else _digest_fd(descriptor)) \
            == receipt.get("sha256")
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _absolute_parts(path: Path) -> tuple:
    absolute = _lexical_absolute(path)
    anchor = Path(absolute.anchor)
    try:
        relative = absolute.relative_to(anchor)
    except ValueError as exc:
        raise OSError("migration path is not absolute") from exc
    parts = tuple(relative.parts)
    if any(part in ("", ".", "..") for part in parts):
        raise OSError("unsafe migration path")
    return absolute, anchor, parts


def _decode_mountinfo_path(value: str) -> str:
    for escaped, literal in (
        ("\\040", " "), ("\\011", "\t"),
        ("\\012", "\n"), ("\\134", "\\"),
    ):
        value = value.replace(escaped, literal)
    return value


def _mount_coordinate(path: Path) -> dict:
    """Linux filesystem-relative coordinate, including bind-mount root.

    Two lexical names can resolve through different bind-mount points to the
    same host subtree. ``mountinfo`` supplies the underlying mount root needed
    to reject that alias before migration scans or writes either tree.
    """
    absolute = _lexical_absolute(path)
    try:
        lines = Path("/proc/self/mountinfo").read_text(
            encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeError) as exc:
        raise OSError("Linux mount identity is unavailable") from exc
    matches = []
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index("-")
            mount_id = int(fields[0])
            device = fields[2]
            root = Path(_decode_mountinfo_path(fields[3]))
            mountpoint = Path(_decode_mountinfo_path(fields[4]))
        except (IndexError, ValueError):
            continue
        if separator < 6 or not root.is_absolute() or not mountpoint.is_absolute():
            continue
        try:
            relative = absolute.relative_to(mountpoint)
        except ValueError:
            continue
        matches.append((len(mountpoint.parts), mount_id, device, root, relative))
    if not matches:
        raise OSError("migration path is outside the visible mount table")
    _depth, mount_id, device, root, relative = max(matches, key=lambda item: item[0])
    object_path = Path(os.path.normpath(os.fspath(root / relative)))
    if not object_path.is_absolute() or ".." in object_path.parts:
        raise OSError("migration mount identity is malformed")
    return {
        "device": device,
        "object_path": os.fspath(object_path),
        "mount_id": mount_id,
    }


def _mount_coordinate_matches(path: Path, receipt) -> bool:
    try:
        current = _mount_coordinate(path)
        return (
            isinstance(receipt, dict)
            and current == {
                "device": str(receipt["device"]),
                "object_path": str(receipt["object_path"]),
                "mount_id": int(receipt["mount_id"]),
            }
        )
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _mount_coordinates_overlap(left, right) -> bool:
    try:
        if left["device"] != right["device"]:
            return False
        left_path = Path(left["object_path"])
        right_path = Path(right["object_path"])
        left_path.relative_to(right_path)
        return True
    except ValueError:
        try:
            right_path.relative_to(left_path)
            return True
        except ValueError:
            return False
    except (KeyError, TypeError):
        raise OSError("migration mount identity is malformed")


def _require_separate_root_receipts(source_receipt, destination_receipt) -> None:
    try:
        source_mount = source_receipt["mount_coordinate"]
        destination_mount = destination_receipt["mount_coordinate"]
    except (KeyError, TypeError) as exc:
        raise OSError("migration root mount proof is missing") from exc
    if _mount_coordinates_overlap(source_mount, destination_mount):
        raise OSError(
            "Source and destination alias or overlap through a mounted view. "
            "Choose genuinely separate folders."
        )


def _capture_root_receipt(path: Path, *, allow_missing=False) -> dict:
    absolute, anchor, parts = _absolute_parts(path)
    descriptors = []
    existing = []
    missing = []
    try:
        base_fd = _open_directory(anchor)
        descriptors.append(base_fd)
        base = _directory_identity(base_fd)
        parent_fd = base_fd
        for index, part in enumerate(parts):
            try:
                child_fd = _open_directory(part, dir_fd=parent_fd)
            except FileNotFoundError:
                if not allow_missing:
                    raise
                missing = list(parts[index:])
                break
            if not _named_entry_matches(parent_fd, part, child_fd):
                os.close(child_fd)
                raise OSError("migration root changed while it was sealed")
            descriptors.append(child_fd)
            existing.append({
                "name": part,
                "identity": _directory_identity(child_fd),
            })
            parent_fd = child_fd
        return {
            "path": os.fspath(absolute),
            "anchor": os.fspath(anchor),
            "anchor_identity": base,
            "existing": existing,
            "missing": missing,
            "mount_coordinate": _mount_coordinate(absolute),
        }
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


class _RootBinding:
    def __init__(self, receipt, descriptors, names):
        self.receipt = receipt
        self.descriptors = descriptors
        self.names = names
        self.bound_identities = [
            _directory_identity(descriptor) for descriptor in descriptors
        ]
        self.trusted_directories = {
            (): self.bound_identities[-1]
        }

    @property
    def root_fd(self):
        return self.descriptors[-1]

    @property
    def path(self) -> Path:
        return Path(self.receipt["path"])

    @property
    def root_mount_id(self) -> int:
        return int(self.bound_identities[-1][6])

    def entry_is_on_root_mount(self, descriptor) -> bool:
        try:
            return _descriptor_mount_id(descriptor) == self.root_mount_id
        except (OSError, TypeError, ValueError):
            return False

    def directory_is_on_root_mount(self, descriptor) -> bool:
        try:
            return _directory_identity(descriptor)[6] == self.root_mount_id
        except (OSError, TypeError, ValueError):
            return False

    def matches_public(self) -> bool:
        if not self.descriptors:
            return False
        try:
            if (
                len(self.bound_identities) != len(self.descriptors)
                or len(self.names) != len(self.descriptors) - 1
                or any(
                    _directory_identity(descriptor) != expected
                    for descriptor, expected in zip(
                        self.descriptors, self.bound_identities)
                )
            ):
                return False
            for parent_fd, name, expected in zip(
                    self.descriptors, self.names,
                    self.bound_identities[1:]):
                if _named_directory_identity(parent_fd, name) != expected:
                    return False
            return True
        except (OSError, TypeError, ValueError):
            return False

    def trust_directory(self, parts, descriptor) -> bool:
        key = tuple(parts)
        identity = _directory_identity(descriptor)
        if identity[6] != self.root_mount_id:
            return False
        existing = self.trusted_directories.get(key)
        if existing is not None and existing != identity:
            return False
        self.trusted_directories[key] = identity
        return True

    def directory_is_trusted(self, parts, descriptor) -> bool:
        identity = _directory_identity(descriptor)
        return (
            identity[6] == self.root_mount_id
            and self.trusted_directories.get(tuple(parts)) == identity
        )

    def close(self) -> None:
        for descriptor in reversed(self.descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        self.descriptors = []


def _refresh_root_receipt(binding: _RootBinding) -> dict:
    receipt = _capture_root_receipt(binding.path)
    try:
        existing = list(receipt["existing"])
        if (
            receipt.get("missing")
            or receipt.get("path") != os.fspath(binding.path)
            or _receipt_directory_identity(receipt.get("anchor_identity"))
            != _directory_identity(binding.descriptors[0])
            or [item["name"] for item in existing] != binding.names
            or len(existing) != len(binding.descriptors) - 1
            or any(
                _receipt_directory_identity(item.get("identity"))
                != _directory_identity(descriptor)
                for item, descriptor in zip(
                    existing, binding.descriptors[1:])
            )
            or not binding.matches_public()
        ):
            raise OSError("migration root changed while it was refreshed")
    except (KeyError, TypeError, ValueError) as exc:
        raise OSError("migration root refresh was malformed") from exc
    return receipt


def _create_directory_noreplace(parent_fd, name) -> int:
    if not _named_entry_missing(parent_fd, name):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), name)
    temporary_name = None
    temporary_fd = None
    for _ in range(16):
        candidate = f".qobuz-migrate-dir-{secrets.token_hex(16)}"
        try:
            os.mkdir(candidate, mode=0o755, dir_fd=parent_fd)
        except FileExistsError:
            continue
        temporary_name = candidate
        temporary_fd = _open_directory(candidate, dir_fd=parent_fd)
        if not _named_entry_matches(parent_fd, candidate, temporary_fd):
            os.close(temporary_fd)
            raise OSError("private migration directory changed")
        break
    if temporary_fd is None or temporary_name is None:
        raise OSError("could not reserve a private migration directory")
    try:
        _rename_noreplace_at(parent_fd, temporary_name, parent_fd, name)
    except BaseException:
        published = _named_entry_matches(parent_fd, name, temporary_fd)
        private = _named_entry_matches(parent_fd, temporary_name, temporary_fd)
        if private:
            try:
                os.rmdir(temporary_name, dir_fd=parent_fd)
            except OSError:
                pass
        if published:
            try:
                _fsync_directories(parent_fd)
            except OSError:
                pass
        os.close(temporary_fd)
        raise
    if (
        not _named_entry_matches(parent_fd, name, temporary_fd)
        or not _named_entry_missing(parent_fd, temporary_name)
    ):
        os.close(temporary_fd)
        raise OSError("migration directory publication was ambiguous")
    _fsync_directories(parent_fd)
    return temporary_fd


def _open_root_receipt(receipt, *, create_missing=False) -> _RootBinding:
    if not isinstance(receipt, dict):
        raise OSError("migration root was not sealed during preview")
    try:
        absolute, anchor, parts = _absolute_parts(Path(receipt["path"]))
        if os.fspath(anchor) != receipt.get("anchor"):
            raise OSError("migration root receipt is malformed")
        existing = list(receipt.get("existing") or [])
        missing = list(receipt.get("missing") or [])
        names = [item["name"] for item in existing] + missing
        if tuple(names) != parts:
            raise OSError("migration root receipt does not match its path")
        if not _mount_coordinate_matches(absolute, receipt.get("mount_coordinate")):
            raise OSError("migration root mount changed after preview")
    except (KeyError, TypeError, ValueError) as exc:
        raise OSError("migration root receipt is malformed") from exc

    descriptors = []
    bound_names = []
    try:
        base_fd = _open_directory(anchor)
        descriptors.append(base_fd)
        if _directory_identity(base_fd) != _receipt_directory_identity(
                receipt.get("anchor_identity")):
            raise OSError("migration filesystem root changed after preview")
        parent_fd = base_fd
        for item in existing:
            name = item["name"]
            child_fd = _open_directory(name, dir_fd=parent_fd)
            if (
                not _named_entry_matches(parent_fd, name, child_fd)
                or _directory_identity(child_fd)
                != _receipt_directory_identity(item.get("identity"))
            ):
                os.close(child_fd)
                raise OSError("migration root changed after preview")
            descriptors.append(child_fd)
            bound_names.append(name)
            parent_fd = child_fd
        if missing and not create_missing:
            raise FileNotFoundError(receipt["path"])
        for name in missing:
            if not _named_entry_missing(parent_fd, name):
                raise OSError("migration root appeared after preview")
            child_fd = _create_directory_noreplace(parent_fd, name)
            descriptors.append(child_fd)
            bound_names.append(name)
            parent_fd = child_fd
        binding = _RootBinding(receipt, descriptors, bound_names)
        descriptors = []
        if not binding.matches_public():
            binding.close()
            raise OSError("migration root changed while it was opened")
        return binding
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _relative_parts(path: Path, root: Path) -> tuple:
    absolute = _lexical_absolute(path)
    try:
        relative = absolute.relative_to(_lexical_absolute(root))
    except ValueError as exc:
        raise OSError("migration source is outside the selected source root") from exc
    parts = tuple(relative.parts)
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise OSError("unsafe migration source path")
    return parts


def _capture_file_receipt(binding: _RootBinding, path: Path, *,
                          hash_content=True) -> dict:
    parts = _relative_parts(path, binding.path)
    descriptors = []
    parent_receipts = []
    parent_fd = binding.root_fd
    file_fd = None
    try:
        for part in parts[:-1]:
            child_fd = _open_directory(part, dir_fd=parent_fd)
            if (
                not _named_entry_matches(parent_fd, part, child_fd)
                or not binding.directory_is_on_root_mount(child_fd)
            ):
                os.close(child_fd)
                raise OSError(
                    "migration path crosses a nested filesystem mount")
            descriptors.append(child_fd)
            parent_receipts.append({
                "name": part,
                "identity": _directory_identity(child_fd),
            })
            parent_fd = child_fd
        file_fd = _open_regular_file(parts[-1], dir_fd=parent_fd)
        if not binding.entry_is_on_root_mount(file_fd):
            raise OSError("migration path crosses a nested filesystem mount")
        before = os.fstat(file_fd)
        digest = _digest_fd(file_fd) if hash_content else None
        after = os.fstat(file_fd)
        if (
            not _same_entry(before, after)
            or not _named_entry_matches(parent_fd, parts[-1], file_fd)
            or not _sealed_directory_chain_matches(
                binding, parent_receipts, descriptors)
            or not binding.matches_public()
        ):
            raise OSError("migration source changed during preview")
        return {
            "relative": list(parts),
            "parents": parent_receipts,
            "file": _file_receipt(file_fd, digest),
        }
    finally:
        if file_fd is not None:
            try:
                os.close(file_fd)
            except OSError:
                pass
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _same_file_snapshot(left, right) -> bool:
    try:
        return (
            left["relative"] == right["relative"]
            and left["parents"] == right["parents"]
            and left["file"]
            == {key: value for key, value in right["file"].items()
                if key != "sha256"}
        )
    except (KeyError, TypeError):
        return False


class _OpenedFile:
    def __init__(self, root, parents, parent_names, parent_fd, name,
                 descriptor, exclusion, verified_digest):
        self.root = root
        self.parents = parents
        self.parent_names = parent_names
        self.parent_fd = parent_fd
        self.name = name
        self.descriptor = descriptor
        self.exclusion = exclusion
        self.verified_digest = verified_digest

    def parent_chain_exact(self) -> bool:
        if not self.root.matches_public():
            return False
        prefix = []
        try:
            for parent_fd, name, child_fd in zip(
                    [self.root.root_fd] + self.parents,
                    self.parent_names,
                    self.parents):
                prefix.append(name)
                identity = _directory_identity(child_fd)
                if (
                    self.root.trusted_directories.get(tuple(prefix))
                    != identity
                    or _named_directory_identity(parent_fd, name) != identity
                ):
                    return False
            return True
        except (OSError, TypeError, ValueError):
            return False

    def namespace_exact(self) -> bool:
        return (
            self.parent_chain_exact()
            and _named_entry_matches(self.parent_fd, self.name, self.descriptor)
        )

    def still_exact(self, receipt) -> bool:
        return (
            self.namespace_exact()
            and self.exclusion is not None
            and self.exclusion.intact()
            and _file_receipt_matches(
                self.descriptor,
                receipt.get("file"),
                digest=self.verified_digest,
            )
        )

    def close(self) -> None:
        if self.exclusion is not None:
            self.exclusion.close()
            self.exclusion = None
        try:
            os.close(self.descriptor)
        except OSError:
            pass
        for descriptor in reversed(self.parents):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _open_file_receipt(binding: _RootBinding, receipt) -> _OpenedFile:
    if not isinstance(receipt, dict):
        raise OSError("migration source was not sealed during preview")
    try:
        parts = tuple(receipt["relative"])
        parents = list(receipt["parents"])
        file_receipt = receipt["file"]
    except (KeyError, TypeError) as exc:
        raise OSError("migration source receipt is malformed") from exc
    if (
        not parts
        or any(not _safe_component(part) for part in parts)
        or not isinstance(file_receipt, dict)
        or any(not isinstance(item, dict) for item in parents)
        or [item.get("name") for item in parents] != list(parts[:-1])
    ):
        raise OSError("migration source receipt is malformed")

    descriptors = []
    file_fd = None
    exclusion = None
    parent_fd = binding.root_fd
    prefix = []
    try:
        for item in parents:
            child_fd = _open_directory(item["name"], dir_fd=parent_fd)
            if (
                not _named_entry_matches(parent_fd, item["name"], child_fd)
                or _directory_identity(child_fd)
                != _receipt_directory_identity(item.get("identity"))
                or not binding.directory_is_on_root_mount(child_fd)
            ):
                os.close(child_fd)
                raise OSError("migration source parent changed after preview")
            descriptors.append(child_fd)
            prefix.append(item["name"])
            if not binding.trust_directory(prefix, child_fd):
                raise OSError(
                    "migration directory conflicts with an earlier proof")
            parent_fd = child_fd
        file_fd = _open_regular_file(parts[-1], dir_fd=parent_fd)
        if not binding.entry_is_on_root_mount(file_fd):
            raise OSError("migration path crosses a nested filesystem mount")
        from qobuz_librarian.file_exclusion import acquire_inode_write_exclusion
        exclusion = acquire_inode_write_exclusion(file_fd)
        if exclusion is None:
            raise OSError("migration source could not be protected from writers")
        verified_digest = _digest_fd(file_fd)
        if (
            not binding.matches_public()
            or not _file_receipt_matches(
                file_fd, file_receipt, digest=verified_digest)
            or not exclusion.intact()
        ):
            raise OSError("migration source changed after preview")
        opened = _OpenedFile(
            binding, descriptors, list(parts[:-1]), parent_fd, parts[-1],
            file_fd, exclusion, verified_digest)
        descriptors = []
        file_fd = None
        exclusion = None
        return opened
    finally:
        if exclusion is not None:
            exclusion.close()
        if file_fd is not None:
            try:
                os.close(file_fd)
            except OSError:
                pass
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _source_root_for_items(items, sources) -> Optional[Path]:
    selected = getattr(items, "source_root", None)
    if selected is not None:
        return _lexical_absolute(Path(selected))
    existing = [
        _lexical_absolute(Path(source)) for source in sources
        if Path(source).exists()
    ]
    if not existing:
        return None
    try:
        common = Path(os.path.commonpath(
            [os.fspath(path.parent) for path in existing]))
    except (OSError, TypeError, ValueError):
        return None
    return common


def _destination_state(root_receipt, dest_root, dest_rel):
    """Return exact preview state without following destination components."""
    parts = tuple(Path(dest_rel).parts)
    if (
        not parts
        or Path(dest_rel).is_absolute()
        or any(part in ("", ".", "..") for part in parts)
    ):
        return True, None, None, "destination path is unsafe"
    try:
        binding = _open_root_receipt(root_receipt, create_missing=False)
    except FileNotFoundError:
        return False, None, {
            "relative": list(parts),
            "parents": [],
            "missing_parents": list(parts[:-1]),
        }, ""
    except OSError:
        return True, None, None, "destination root is unsafe"
    descriptors = []
    parent_receipts = []
    parent_fd = binding.root_fd
    try:
        for index, part in enumerate(parts[:-1]):
            try:
                child_fd = _open_directory(part, dir_fd=parent_fd)
            except FileNotFoundError:
                return False, None, {
                    "relative": list(parts),
                    "parents": parent_receipts,
                    "missing_parents": list(parts[index:-1]),
                }, ""
            if (
                not _named_entry_matches(parent_fd, part, child_fd)
                or not binding.directory_is_on_root_mount(child_fd)
            ):
                os.close(child_fd)
                return (
                    True, None, None,
                    "destination path crosses a nested filesystem mount",
                )
            descriptors.append(child_fd)
            parent_receipts.append({
                "name": part,
                "identity": _directory_identity(child_fd),
            })
            parent_fd = child_fd
        try:
            value = os.stat(parts[-1], dir_fd=parent_fd,
                            follow_symlinks=False)
        except FileNotFoundError:
            return False, None, {
                "relative": list(parts),
                "parents": parent_receipts,
                "missing_parents": [],
            }, ""
        if not stat.S_ISREG(value.st_mode):
            return (
                True, None, None,
                "destination exists but is not a regular file",
            )
        receipt = _capture_file_receipt(
            binding, Path(dest_root) / Path(dest_rel))
        return True, receipt, None, "destination already exists"
    except OSError:
        return True, None, None, "destination path is unsafe"
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        binding.close()


def _probe_destination_name_semantics(dest_root: Path) -> dict:
    """Measure case and Unicode-normalization aliasing on the target FS."""
    anchor = _existing_ancestor(dest_root)
    if anchor is None:
        raise OSError("destination name semantics could not be measured")
    receipt = _capture_root_receipt(anchor)
    binding = _open_root_receipt(receipt)
    private_name = private_fd = probe_fd = None
    probe_name = f"ql-probe-{secrets.token_hex(12)}-A-é"
    try:
        private_name, private_fd = _reserve_private_directory(
            binding.root_fd, prefix="qobuz-migrate-name-probe")
        probe_fd = os.open(
            probe_name,
            os.O_RDONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=private_fd,
        )
        if not _named_entry_matches(private_fd, probe_name, probe_fd):
            raise OSError("destination name probe changed unexpectedly")

        def _aliases(candidate):
            try:
                value = os.stat(
                    candidate, dir_fd=private_fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            return _same_entry(value, os.fstat(probe_fd))

        case_sensitive = not _aliases(probe_name.lower())
        normalization_sensitive = not _aliases(
            unicodedata.normalize("NFD", probe_name))
        return {
            "case_sensitive": case_sensitive,
            "normalization_sensitive": normalization_sensitive,
        }
    finally:
        cleanup_error = None
        if probe_fd is not None:
            try:
                if _named_entry_matches(private_fd, probe_name, probe_fd):
                    os.unlink(probe_name, dir_fd=private_fd)
                if not _named_entry_missing(private_fd, probe_name):
                    cleanup_error = OSError(
                        "destination name probe could not be removed")
            except BaseException as exc:
                cleanup_error = exc
            try:
                os.close(probe_fd)
            except OSError:
                pass
        if private_fd is not None:
            try:
                if (
                    not os.listdir(private_fd)
                    and private_name is not None
                    and _named_entry_matches(
                        binding.root_fd, private_name, private_fd)
                ):
                    os.rmdir(private_name, dir_fd=binding.root_fd)
                    _fsync_directories(binding.root_fd)
                if (
                    private_name is not None
                    and not _named_entry_missing(binding.root_fd, private_name)
                ):
                    cleanup_error = cleanup_error or OSError(
                        "destination name-probe directory was retained")
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
            try:
                os.close(private_fd)
            except OSError:
                pass
        binding.close()
        if cleanup_error is not None:
            raise OSError(
                "destination name semantics could not be measured safely"
            ) from cleanup_error


def _destination_collision_key(path: Path, semantics) -> tuple:
    if not isinstance(semantics, dict):
        raise OSError("destination name semantics were not measured")
    try:
        if (
            type(semantics["case_sensitive"]) is not bool
            or type(semantics["normalization_sensitive"]) is not bool
        ):
            raise ValueError
        case_sensitive = semantics["case_sensitive"]
        normalization_sensitive = semantics["normalization_sensitive"]
    except (KeyError, TypeError, ValueError) as exc:
        raise OSError("destination name semantics proof is malformed") from exc
    values = []
    for part in Path(path).parts:
        value = part
        if not normalization_sensitive:
            value = unicodedata.normalize("NFD", value)
        if not case_sensitive:
            value = value.casefold()
        values.append(value)
    return tuple(values)


def _capture_companion_receipts(binding, entries) -> list:
    folders = {
        tuple(entry.source_receipt.get("relative", ())[:-1])
        for entry in entries
        if entry.dest_rel is not None and entry.source_receipt is not None
    }
    receipts = []
    seen = set()
    for folder in folders:
        descriptors = []
        parent_fd = binding.root_fd
        try:
            for part in folder:
                child_fd = _open_directory(part, dir_fd=parent_fd)
                if (
                    not _named_entry_matches(parent_fd, part, child_fd)
                    or not binding.directory_is_on_root_mount(child_fd)
                ):
                    os.close(child_fd)
                    raise OSError(
                        "migration source crosses a nested filesystem mount")
                descriptors.append(child_fd)
                parent_fd = child_fd
            before_names = sorted(
                os.fsencode(name) for name in os.listdir(parent_fd))
            parent_receipts = [
                {"name": part, "identity": _directory_identity(descriptor)}
                for part, descriptor in zip(folder, descriptors)
            ]
            for encoded_name in before_names:
                name = os.fsdecode(encoded_name)
                if Path(name).suffix.lower() not in _COMPANION_EXTS:
                    continue
                value = os.stat(
                    name, dir_fd=parent_fd, follow_symlinks=False)
                if not stat.S_ISREG(value.st_mode):
                    continue
                _path, _metadata, receipt = _scan_file_from_descriptor(
                    binding,
                    parent_fd,
                    parent_receipts,
                    folder + (name,),
                )
                key = tuple(receipt["relative"])
                if key not in seen:
                    seen.add(key)
                    receipts.append(receipt)
            after_names = sorted(
                os.fsencode(name) for name in os.listdir(parent_fd))
            if (
                before_names != after_names
                or not _sealed_directory_chain_matches(
                    binding, parent_receipts, descriptors)
            ):
                raise OSError(
                    "migration companion directory changed during scan")
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
    return receipts


def build_plan(items, dest_root: Path) -> MigrationPlan:
    """Turn ``(source, meta, source_of_truth)`` tuples into a full plan.

    It seals the source and destination filesystem state used by the preview.
    Files with unusable metadata are marked
    ``UNPLACEABLE``; two distinct sources resolving to one destination - or a
    destination that already exists on disk - are all marked ``COLLISION`` and
    skipped. Multi-disc layout is inferred per album from the disc numbers seen.
    """
    dest_root = Path(dest_root)
    raw_items = list(items)
    sources = [row[0] for row in raw_items if len(row) >= 1]
    source_root = _source_root_for_items(items, sources)
    source_root_receipt = getattr(items, "source_root_receipt", None)
    if source_root is not None and source_root_receipt is None:
        try:
            source_root_receipt = _capture_root_receipt(source_root)
        except OSError:
            source_root_receipt = None
    try:
        dest_root_receipt = _capture_root_receipt(
            dest_root, allow_missing=True)
    except OSError:
        dest_root_receipt = None
    if source_root_receipt is not None and dest_root_receipt is not None:
        _require_separate_root_receipts(
            source_root_receipt, dest_root_receipt)
    destination_name_semantics = _probe_destination_name_semantics(dest_root)

    source_binding = None
    if source_root_receipt is not None:
        try:
            source_binding = _open_root_receipt(source_root_receipt)
        except OSError:
            source_binding = None
    placeable, entries = [], []
    try:
        for row in raw_items:
            if len(row) == 4:
                source, meta, sot, source_receipt = row
            else:
                source, meta, sot = row
                source_receipt = None
            source = Path(source)
            if source_receipt is None and source_binding is not None:
                try:
                    source_receipt = _capture_file_receipt(
                        source_binding, source)
                except OSError:
                    source_receipt = None
            if not is_placeable(meta):
                entries.append(PlanEntry(
                    source=source, status=UNPLACEABLE, source_of_truth=sot,
                    reason="no usable artist/album tags", meta=meta,
                    source_receipt=source_receipt))
                continue
            if source_root_receipt is not None and source_receipt is None:
                entries.append(PlanEntry(
                    source=source, status=UNPLACEABLE, source_of_truth=sot,
                    reason="source could not be sealed safely", meta=meta))
                continue
            placeable.append((source, meta, sot, source_receipt))
    finally:
        if source_binding is not None:
            source_binding.close()

    # Group by album so multi-disc can be decided from the whole album, not one
    # track: a disc tag of 2 only means "Disc 2/" if the album really spans
    # discs (disctotal > 1, or some track carries a higher disc number).
    groups: dict = {}
    for source, meta, sot, source_receipt in placeable:
        groups.setdefault(_album_key(meta), []).append(
            (source, meta, sot, source_receipt))

    seen_dest: dict = {}
    built = []
    for key, members in groups.items():
        artist_folder, album_folder = key
        multidisc = any(
            (m.get("disctotal") or 0) > 1 or (m.get("disc") or 1) > 1
            for _, m, _, _ in members)
        for source, meta, sot, source_receipt in members:
            rel = Path(artist_folder) / album_folder
            if multidisc:
                rel = rel / f"Disc {meta.get('disc') or 1}"
            rel = rel / track_filename(meta)
            built.append((source, sot, meta, rel, source_receipt))
            collision_key = _destination_collision_key(
                rel, destination_name_semantics)
            seen_dest.setdefault(collision_key, []).append(source)

    for source, sot, meta, rel, source_receipt in built:
        collision_key = _destination_collision_key(
            rel, destination_name_semantics)
        if len(seen_dest[collision_key]) > 1:
            entries.append(PlanEntry(
                source=source, status=COLLISION, dest_rel=rel,
                source_of_truth=sot,
                reason="two files map to the same destination", meta=meta,
                source_receipt=source_receipt))
            continue
        if dest_root_receipt is None:
            entries.append(PlanEntry(
                source=source, status=COLLISION, dest_rel=rel,
                source_of_truth=sot,
                reason="destination root could not be sealed safely", meta=meta,
                source_receipt=source_receipt))
            continue
        exists, destination_receipt, destination_path_receipt, reason = (
            _destination_state(
            dest_root_receipt, dest_root, rel)
        )
        if exists:
            entries.append(PlanEntry(
                source=source, status=COLLISION, dest_rel=rel,
                source_of_truth=sot,
                reason=reason, meta=meta, source_receipt=source_receipt,
                destination_receipt=destination_receipt))
        else:
            entries.append(PlanEntry(
                source=source, status=PLACE, dest_rel=rel,
                source_of_truth=sot, reason="", meta=meta,
                source_receipt=source_receipt,
                destination_path_receipt=destination_path_receipt))

    companion_receipts = list(
        getattr(items, "companion_receipts", ()) or ())
    if not companion_receipts and source_root_receipt is not None:
        try:
            source_binding = _open_root_receipt(source_root_receipt)
            companion_receipts = _capture_companion_receipts(
                source_binding, entries)
        except OSError:
            companion_receipts = []
        finally:
            if source_binding is not None:
                source_binding.close()
    mapped_source_folders = {
        tuple(entry.source_receipt.get("relative", ())[:-1])
        for entry in entries
        if entry.dest_rel is not None and isinstance(entry.source_receipt, dict)
    }
    companion_receipts = [
        receipt for receipt in companion_receipts
        if isinstance(receipt, dict)
        and tuple(receipt.get("relative", ())[:-1]) in mapped_source_folders
    ]

    return MigrationPlan(
        dest_root=dest_root,
        entries=entries,
        source_root=source_root,
        source_root_receipt=source_root_receipt,
        dest_root_receipt=dest_root_receipt,
        companion_receipts=companion_receipts,
        destination_name_semantics=destination_name_semantics,
    )


# ── AcoustID second stage (opt-in, container-only) ───────────────────────────

def choose_acoustid_match(candidates, min_score: float = ACOUSTID_MIN_SCORE):
    """Pick a single confident match from AcoustID candidates, or None.

    Pure decision logic, kept apart from the network call so it can be tested
    without a fingerprinter. Returns None when the best score is below
    ``min_score`` (too weak) or when two strong candidates name different
    artists (ambiguous) - both cases mean "leave it and report"."""
    strong = [c for c in candidates if (c.get("score") or 0) >= min_score]
    if not strong:
        return None
    strong.sort(key=lambda c: -(c.get("score") or 0))
    best = strong[0]
    artists = {normalize(c.get("albumartist") or c.get("artist") or "")
               for c in strong}
    artists.discard("")
    if len(artists) > 1:
        return None
    return best


def _first_name(artists) -> str:
    if isinstance(artists, list) and artists:
        return artists[0].get("name") or ""
    return ""


def _pick_album(recording: dict) -> tuple:
    """(album, year, albumartist, compilation) from a matched recording's
    release groups, or ("", 0, "", False) if it lists none.

    Prefers a primary-type "Album" over singles/EPs, takes the earliest release
    year, and flags it a compilation when the release group says so or the album
    artist is a Various-Artists alias."""
    groups = recording.get("releasegroups") or []
    if not groups:
        return "", 0, "", False
    groups = sorted(groups, key=lambda g: 0 if (g.get("type") or "").lower() == "album" else 1)
    rg = groups[0]
    album = rg.get("title") or ""
    albumartist = _first_name(rg.get("artists"))
    secondary = [s.lower() for s in (rg.get("secondarytypes") or [])]
    compilation = ((rg.get("type") or "").lower() == "compilation"
                   or "compilation" in secondary
                   or normalize(albumartist) in VA_NORMALIZED)
    year = 0
    for rel in (rg.get("releases") or []):
        y = (rel.get("date") or {}).get("year") or 0
        if y and (year == 0 or y < year):
            year = y
    return album, year, albumartist, compilation


def identify_from_lookup(resp: dict, min_score: float, stem: str,
                         ext: str) -> Optional[dict]:
    """Turn a raw AcoustID lookup response into placement metadata, or None.

    Pure (no network), so the confidence/ambiguity/album-selection logic is
    testable without a fingerprinter. Returns None when no result clears
    ``min_score`` or is ambiguous, and also when a confident recording lists no
    album - identifying a recording without a release still gives no folder to
    build, so the file stays unplaceable rather than guessed at."""
    candidates = []
    for r in (resp.get("results") or []):
        recs = r.get("recordings") or []
        rec = recs[0] if recs else {}
        candidates.append({
            "score": r.get("score") or 0,
            "artist": _first_name(rec.get("artists")),
            "_rec": rec,
        })
    best = choose_acoustid_match(candidates, min_score)
    if not best:
        return None
    rec = best.get("_rec") or {}
    album, year, albumartist, compilation = _pick_album(rec)
    if not album:
        return None
    return {
        "albumartist": albumartist or best.get("artist") or "",
        "album": album,
        "title": rec.get("title") or stem,
        "track": 0, "disc": 1, "disctotal": 0, "year": year,
        "compilation": compilation, "ext": ext.lower(),
    }


def fingerprint_identify(path: Path, min_score: float = ACOUSTID_MIN_SCORE,
                         ext: str = "", *, descriptor=None) -> Optional[dict]:
    """Identify one file by audio fingerprint via AcoustID, resolving an album
    so the file can actually be placed. None if no confident match (or the
    fingerprinter isn't available).

    Lazily imports ``acoustid`` - it and ``fpcalc`` ship only in the container,
    so this whole stage is a no-op on a host without them. The lookup asks for
    release groups + releases so a recording match yields an album and year, not
    just a title."""
    try:
        import acoustid
    except ImportError:
        vlog("acoustid not installed; skipping fingerprint stage")
        return None
    try:
        if descriptor is None:
            duration, fp = acoustid.fingerprint_file(str(path))
        else:
            completed = subprocess.run(
                ["fpcalc", "-json", f"/proc/self/fd/{int(descriptor)}"],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
                pass_fds=(int(descriptor),),
            )
            payload = json.loads(completed.stdout)
            duration = payload["duration"]
            fp = payload["fingerprint"]
        resp = acoustid.lookup(ACOUSTID_API_KEY, fp, duration,
                               meta="recordings releasegroups releases")
    except Exception as e:
        vlog(f"fingerprint failed for {path.name}: {e}")
        return None
    return identify_from_lookup(resp, min_score, path.stem, (ext or path.suffix).lower())


# ── Path validation ───────────────────────────────────────────────────────────

def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def validate_paths(src: Path, dest: Path, *,
                   in_place: bool = False) -> Optional[str]:
    """Reason the source/destination pair is unusable, or None if it's fine.

    The destination has to be a separate tree from the source so a copy can't
    recurse into or overwrite itself."""
    if not src.is_dir():
        return f"Source isn't a readable directory: {src}"
    if in_place and not os.access(str(src), os.W_OK):
        return (f"Source is not writable - in-place mode moves files out of "
                f"the source tree and requires write access: {src}")
    if dest.exists() and not dest.is_dir():
        return f"Destination exists but isn't a folder: {dest}"
    if src.resolve() == dest.resolve():
        return "Source and destination are the same folder."
    if _is_within(dest, src):
        return ("Destination is inside the source - that would copy the new "
                "library into itself. Choose a destination outside the source.")
    if _is_within(src, dest):
        return "Source is inside the destination. Choose separate folders."
    try:
        source_receipt = _capture_root_receipt(src)
        destination_receipt = _capture_root_receipt(dest, allow_missing=True)
        _require_separate_root_receipts(source_receipt, destination_receipt)
    except OSError as exc:
        return f"Source/destination safety could not be proved: {exc}"
    # The copy/move writes into the destination tree (created if absent), so a
    # read-only dest must fail here, not partway through with a raw OSError.
    dest_anchor = dest if dest.exists() else _existing_ancestor(dest)
    if dest_anchor is None or not os.access(str(dest_anchor), os.W_OK):
        return f"Destination isn't writable: {dest_anchor or dest}"
    return None


def _existing_ancestor(path: Path) -> Optional[Path]:
    """The nearest path that exists at or above ``path`` (where a copy lands)."""
    p = Path(path)
    while True:
        if p.exists():
            return p
        if p.parent == p:
            return None
        p = p.parent


def space_estimate(plan: MigrationPlan, *, in_place: bool = False,
                   resume_entries=None) -> tuple:
    """``(bytes_to_write, free_bytes_at_dest)`` for carrying out ``plan``.

    Only files that actually get written count toward ``bytes_to_write``:
    every placed audio file (in-place mode also creates and flushes a verified
    destination before retiring its source), plus companions for both new
    placements and verified resumes.
    ``free_bytes`` is the space available where the new library is built, or
    None when it can't be read (no existing ancestor, or a stat error)."""
    anchor = _existing_ancestor(plan.dest_root)
    free = None
    if anchor is not None:
        try:
            free = shutil.disk_usage(anchor).free
        except OSError:
            free = None
    need = 0
    # source folder -> set of dest folders that will receive its audio, mirroring
    # _carry_companion_files's folder_map so the companion accounting below
    # matches what execution actually writes (incl. AcoustID fan-out).
    companion_targets: dict = {}
    for entry in plan.placed:
        try:
            need += int(entry.source_receipt["file"]["size"])
            source_folder = tuple(
                entry.source_receipt["relative"][:-1])
        except (KeyError, TypeError, ValueError):
            continue
        if entry.dest_rel is not None:
            companion_targets.setdefault(source_folder, set()).add(
                (plan.dest_root / entry.dest_rel).parent)
    # An existing, identical destination needs no audio bytes, but a resumed
    # run can still carry art and sidecars from its source album folder. A
    # caller may supply the already-reviewed mappings so this estimate does not
    # reread every audio pair; execution still verifies each mapping before it
    # can authorise a companion copy.
    if resume_entries is None:
        resume_entries = verified_resume_entries(plan)
    for entry in resume_entries:
        if entry.dest_rel is not None and entry.source_receipt is not None:
            source_folder = tuple(entry.source_receipt["relative"][:-1])
            companion_targets.setdefault(source_folder, set()).add(
                (plan.dest_root / entry.dest_rel).parent)
    # Non-audio companions (cover art, booklets, .cue/.log/.lrc, playlists) are
    # ALWAYS copied into each destination folder, so their bytes count alongside
    # the audio. Without this the preview understates the space a library with
    # large booklets or scans needs.
    companion_sizes: dict = {}
    for receipt in plan.companion_receipts:
        try:
            folder = tuple(receipt["relative"][:-1])
            companion_sizes[folder] = companion_sizes.get(folder, 0) + int(
                receipt["file"]["size"])
        except (KeyError, TypeError, ValueError):
            continue
    for src_folder, dst_folders in companion_targets.items():
        comp_bytes = companion_sizes.get(src_folder, 0)
        need += comp_bytes * len(dst_folders)
    return need, free


# ── Source collection ─────────────────────────────────────────────────────────

# Non-audio files worth carrying with an album: cover art, embedded booklets,
# rip logs, cue sheets, synced lyrics, and playlists. Only audio gets a plan
# entry, so without an explicit pass these are stranded in the old library.
_COMPANION_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp",
    ".cue", ".log", ".pdf", ".lrc", ".nfo", ".m3u", ".m3u8",
}


class CollectedItems(list):
    """Migration scan results carrying the exact selected-root evidence."""

    def __init__(self, source_root, source_root_receipt):
        super().__init__()
        self.source_root = Path(source_root)
        self.source_root_receipt = source_root_receipt
        self.companion_receipts = []


def _scan_file_from_descriptor(binding, parent_fd, parent_receipts,
                               relative, *, read_tags=False):
    """Seal one named regular file and optionally read tags from its exact fd."""
    name = relative[-1]
    descriptor = None
    exclusion = None
    try:
        descriptor = _open_regular_file(name, dir_fd=parent_fd)
        if not binding.entry_is_on_root_mount(descriptor):
            raise OSError("migration source crosses a nested filesystem mount")
        from qobuz_librarian.file_exclusion import acquire_inode_write_exclusion
        exclusion = acquire_inode_write_exclusion(descriptor)
        if exclusion is None or not exclusion.intact():
            raise OSError("migration source could not be protected from writers")
        before = {
            "relative": list(relative),
            "parents": list(parent_receipts),
            "file": _file_receipt(descriptor),
        }
        display_path = binding.path.joinpath(*relative)
        metadata = extract_metadata(
            display_path, descriptor=descriptor) if read_tags else None
        digest = _digest_fd(descriptor)
        after = {
            "relative": list(relative),
            "parents": list(parent_receipts),
            "file": _file_receipt(descriptor, digest),
        }
        if (
            not _same_file_snapshot(before, after)
            or not _named_entry_matches(parent_fd, name, descriptor)
            or not exclusion.intact()
            or not binding.matches_public()
        ):
            raise OSError("migration source changed while it was reviewed")
        return display_path, metadata, after
    finally:
        if exclusion is not None:
            exclusion.close()
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _enumerate_source_descriptors(binding, *, cancel_check=None,
                                  progress=None):
    """Walk only held no-follow directory fds; never reopen a public ancestor."""
    audio = []
    companions = []
    audio_exts = set(config.AUDIO_EXTS)
    visited = 0

    def walk(parent_fd, relative_parent, parent_receipts, parent_descriptors):
        nonlocal visited
        before_names = sorted(os.fsencode(name) for name in os.listdir(parent_fd))
        for encoded_name in before_names:
            if cancel_check is not None and cancel_check():
                return
            name = os.fsdecode(encoded_name)
            if not _safe_component(name):
                raise OSError("migration source contains an unsafe filename")
            value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            relative = relative_parent + (name,)
            if stat.S_ISDIR(value.st_mode):
                child_fd = _open_directory(name, dir_fd=parent_fd)
                try:
                    if (
                        not _named_entry_matches(parent_fd, name, child_fd)
                        or not binding.directory_is_on_root_mount(child_fd)
                    ):
                        raise OSError(
                            "migration source crosses a nested filesystem mount")
                    receipt = {
                        "name": name,
                        "identity": _directory_identity(child_fd),
                    }
                    walk(
                        child_fd,
                        relative,
                        parent_receipts + [receipt],
                        parent_descriptors + [child_fd],
                    )
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(value.st_mode):
                suffix = Path(name).suffix.lower()
                if suffix not in audio_exts and suffix not in _COMPANION_EXTS:
                    continue
                visited += 1
                if progress:
                    progress(
                        "Reading tags" if suffix in audio_exts
                        else "Sealing cover art and sidecars",
                        visited, 0, name,
                    )
                path, metadata, receipt = _scan_file_from_descriptor(
                    binding,
                    parent_fd,
                    parent_receipts,
                    relative,
                    read_tags=suffix in audio_exts,
                )
                if suffix in audio_exts:
                    audio.append((path, metadata, receipt))
                else:
                    companions.append(receipt)
        after_names = sorted(os.fsencode(name) for name in os.listdir(parent_fd))
        if (
            before_names != after_names
            or not _sealed_directory_chain_matches(
                binding, parent_receipts, parent_descriptors)
        ):
            raise OSError("migration source changed during directory enumeration")

    walk(binding.root_fd, (), [], [])
    if not binding.matches_public():
        raise OSError("migration source root changed during enumeration")
    audio.sort(key=lambda item: os.fsencode(item[0]))
    companions.sort(key=lambda item: tuple(
        os.fsencode(part) for part in item["relative"]))
    return audio, companions


def _carry_companion_files(plan: "MigrationPlan", result: "ExecResult", *,
                           entry_map,
                           resume_entries=None,
                           progress: Optional[Callable] = None,
                           cancel_check: Optional[Callable[[], bool]] = None,
                           source_root_binding=None,
                           destination_root_binding=None,
                           ) -> None:
    """Copy each migrated album folder's non-audio companions into the
    destination folder(s) that received its audio. Always a copy (never a
    move), even in in-place mode: the same cover can fan out to several
    destinations (for example, AcoustID splitting one source folder), the
    source folder may still hold audio that failed/skipped, and a duplicated
    cover image is harmless.
    """
    folder_map: dict = {}

    def _cancelled() -> bool:
        if cancel_check is not None and cancel_check():
            result.cancelled = True
        return result.cancelled

    def _remember(entry):
        if (
            entry is None
            or entry.dest_rel is None
            or entry.source_receipt is None
            or _cancelled()
        ):
            return False
        source_folder = tuple(entry.source_receipt["relative"][:-1])
        folder_map.setdefault(source_folder, set()).add(
            Path(entry.dest_rel).parent)
        return True

    for source, dest_rel, status, _reason in result.outcomes:
        if _cancelled():
            return
        if status == COPIED:
            _remember(entry_map.get(_plan_entry_key(source, dest_rel)))

    # Planning keeps a destination mapping only for the unambiguous
    # "already exists" collision. Re-check it here rather than trusting the
    # preview-time result; the file may have changed while the user reviewed
    # the plan. Other collision classes never become companion targets.
    if resume_entries is None:
        resume_entries = [
            entry for entry in plan.collisions if _is_resume_collision(entry)
        ]
    else:
        resume_entries = [
            entry for entry in resume_entries if _is_resume_collision(entry)
        ]
    total_resumes = len(resume_entries)
    for index, entry in enumerate(resume_entries, 1):
        if _cancelled():
            return
        if progress:
            progress(
                "Rechecking existing copies", index, total_resumes,
                entry.source.name,
            )
        if _cancelled():
            return
        remembered = False
        opened_source = opened_destination = None
        fatal_exception = None
        cleanup_primary = None
        cleanup_failures = []
        outcome_recorded = False
        try:
            if (
                source_root_binding is None
                or destination_root_binding is None
                or entry.source_receipt is None
                or entry.destination_receipt is None
                or tuple(entry.source_receipt.get("relative") or ())
                != _relative_parts(entry.source, source_root_binding.path)
                or tuple(entry.destination_receipt.get("relative") or ())
                != tuple(Path(entry.dest_rel).parts)
            ):
                raise OSError("existing destination was not sealed")
            opened_source = _open_file_receipt(
                source_root_binding, entry.source_receipt)
            opened_destination = _open_file_receipt(
                destination_root_binding, entry.destination_receipt)
            if (
                opened_source.verified_digest
                != opened_destination.verified_digest
                or not opened_source.still_exact(entry.source_receipt)
                or not opened_destination.still_exact(
                    entry.destination_receipt)
            ):
                raise OSError("existing destination changed after preview")
            remembered = _remember(entry)
        except KeyboardInterrupt as exc:
            result.cancelled = True
            cleanup_primary = exc
            result.failed += 1
            result.outcomes.append((
                entry.source,
                entry.dest_rel,
                FAILED,
                "migration was interrupted",
            ))
            outcome_recorded = True
        except OSError:
            remembered = False
        except BaseException as exc:
            result.cancelled = True
            fatal_exception = exc
            result.failed += 1
            result.outcomes.append((
                entry.source,
                entry.dest_rel,
                FAILED,
                str(exc) or type(exc).__name__,
            ))
            outcome_recorded = True
        finally:
            if opened_destination is not None:
                _capture_cleanup(
                    cleanup_failures,
                    "existing-copy destination teardown",
                    opened_destination.close,
                )
            if opened_source is not None:
                _capture_cleanup(
                    cleanup_failures,
                    "existing-copy source teardown",
                    opened_source.close,
                )
        if not outcome_recorded:
            if remembered:
                result.skipped += 1
                result.outcomes.append((
                    entry.source, entry.dest_rel, SKIPPED,
                    "verified existing copy"))
            else:
                result.failed += 1
                result.outcomes.append((
                    entry.source, entry.dest_rel, FAILED,
                    "existing destination could not be reverified"))
        _raise_cleanup_abort(
            result,
            cleanup_failures,
            primary=(
                fatal_exception
                if fatal_exception is not None
                else cleanup_primary
            ),
        )
        if fatal_exception is not None:
            raise MigrationExecutionAbort(
                result, fatal_exception) from fatal_exception
        if _cancelled():
            return

    receipts_by_folder: dict = {}
    for receipt in plan.companion_receipts:
        try:
            folder = tuple(receipt["relative"][:-1])
        except (KeyError, TypeError):
            continue
        receipts_by_folder.setdefault(folder, []).append(receipt)

    for src_folder, dst_folders in folder_map.items():
        if _cancelled():
            return
        companions = receipts_by_folder.get(src_folder, ())
        for dst_folder in dst_folders:
            for receipt in companions:
                if _cancelled():
                    return
                opened = None
                published = None
                discard_publication = False
                fatal_exception = None
                cleanup_primary = None
                cleanup_failures = []
                name = Path(receipt["relative"][-1]).name
                source_path = source_root_binding.path.joinpath(
                    *receipt["relative"])
                destination_path = Path(dst_folder) / name
                try:
                    if progress:
                        progress(
                            "Carrying cover art and sidecars", 0, 0, name)
                    if _cancelled():
                        return
                    opened = _open_file_receipt(
                        source_root_binding, receipt)
                    published = _stream_copy_noreplace(
                        opened,
                        receipt,
                        destination_root_binding,
                        destination_path,
                    )
                    result.companions += 1
                    result.companion_outcomes.append((
                        source_path, destination_path, COPIED, ""))
                except _PublishedCopyFailure as exc:
                    published = exc.published
                    discard_publication = True
                    result.companion_outcomes.append((
                        source_path,
                        destination_path,
                        FAILED,
                        str(exc.cause),
                    ))
                    if exc.interrupted:
                        result.cancelled = True
                        cleanup_primary = exc.cause
                    elif not isinstance(exc.cause, Exception):
                        result.cancelled = True
                        fatal_exception = exc.cause
                except FileExistsError:
                    result.companion_outcomes.append((
                        source_path,
                        destination_path,
                        SKIPPED,
                        "destination already exists",
                    ))
                except KeyboardInterrupt as exc:
                    discard_publication = published is not None
                    result.cancelled = True
                    cleanup_primary = exc
                    result.companion_outcomes.append((
                        source_path,
                        destination_path,
                        FAILED,
                        "migration was interrupted",
                    ))
                except (OSError, shutil.Error) as exc:
                    log.info(f"  ⚠  couldn't carry {name}: {exc}")
                    result.companion_outcomes.append((
                        source_path,
                        destination_path,
                        FAILED,
                        str(exc),
                    ))
                except BaseException as exc:
                    discard_publication = published is not None
                    result.cancelled = True
                    result.companion_outcomes.append((
                        source_path,
                        destination_path,
                        FAILED,
                        str(exc) or type(exc).__name__,
                    ))
                    fatal_exception = exc
                finally:
                    if discard_publication and published is not None:
                        discarded = False
                        try:
                            discarded = _discard_owned_publication(
                                published,
                                receipt["file"],
                                recoveries=result.recoveries,
                            )
                        except BaseException as exc:
                            cleanup_failures.append((
                                "companion publication rollback", exc))
                        if not discarded:
                            try:
                                recovery = _owned_publication_recovery(
                                    published,
                                    source_path,
                                    receipt,
                                    "companion publication was interrupted and "
                                    "the exact same-run destination could not be "
                                    "removed safely",
                                )
                                if recovery not in result.recoveries:
                                    result.recoveries.append(recovery)
                            except BaseException as exc:
                                cleanup_failures.append((
                                    "companion recovery recording", exc))
                    _capture_cleanup(
                        cleanup_failures,
                        "companion destination teardown",
                        lambda: _close_published_file(published),
                    )
                    if opened is not None:
                        _capture_cleanup(
                            cleanup_failures,
                            "companion source teardown",
                            opened.close,
                        )
                _raise_cleanup_abort(
                    result,
                    cleanup_failures,
                    primary=(
                        fatal_exception
                        if fatal_exception is not None
                        else cleanup_primary
                    ),
                )
                if fatal_exception is not None:
                    raise MigrationExecutionAbort(
                        result, fatal_exception) from fatal_exception


def collect_items(source_root: Path, *, use_acoustid: bool = False,
                  cancel_check: Optional[Callable[[], bool]] = None,
                  progress: Optional[Callable] = None,
                  min_score: float = ACOUSTID_MIN_SCORE) -> list:
    from qobuz_librarian.file_exclusion import inode_write_exclusion_scope

    with inode_write_exclusion_scope() as available:
        if not available:
            raise OSError(
                "migration scan could not initialise writer protection")
        return _collect_items(
            source_root,
            use_acoustid=use_acoustid,
            cancel_check=cancel_check,
            progress=progress,
            min_score=min_score,
        )


def _collect_items(source_root: Path, *, use_acoustid: bool = False,
                   cancel_check: Optional[Callable[[], bool]] = None,
                   progress: Optional[Callable] = None,
                   min_score: float = ACOUSTID_MIN_SCORE) -> list:
    """Walk the source and return ``(path, meta, source_of_truth)`` tuples.

    Stage 1 reads tags for every file. Stage 2 (only when ``use_acoustid`` is
    set) fingerprints just the files tags couldn't place, so the slow,
    network-bound path never touches the well-tagged majority."""
    import time

    source_root = _lexical_absolute(Path(source_root))
    root_receipt = _capture_root_receipt(source_root)
    root_binding = _open_root_receipt(root_receipt)
    try:
        items = CollectedItems(source_root, root_receipt)
        scanned, items.companion_receipts = _enumerate_source_descriptors(
            root_binding,
            cancel_check=cancel_check,
            progress=progress,
        )
        # (path, orig_meta, receipt) - orig_meta may retain track/disc data for
        # an AcoustID result, while receipt is the exact file reviewed.
        unplaced = []
        for i, (f, meta, receipt) in enumerate(scanned, 1):
            if cancel_check and cancel_check():
                break
            if is_placeable(meta):
                items.append((f, meta, "tags", receipt))
            else:
                unplaced.append((f, meta, receipt))

        if use_acoustid and unplaced:
            n = len(unplaced)
            for i, (f, orig_meta, receipt) in enumerate(unplaced, 1):
                if cancel_check and cancel_check():
                    items.append((f, None, "", receipt))
                    continue
                if progress:
                    progress("Fingerprinting unidentified files", i, n, f.name)
                opened = None
                try:
                    opened = _open_file_receipt(root_binding, receipt)
                    meta = fingerprint_identify(
                        f,
                        min_score=min_score,
                        ext=f.suffix,
                        descriptor=opened.descriptor,
                    )
                    if not opened.still_exact(receipt):
                        meta = None
                except OSError:
                    meta = None
                finally:
                    if opened is not None:
                        opened.close()
                if meta and orig_meta:
                    # Prefer the file's own track/disc numbers over AcoustID's
                    # zero-defaults: the lookup has no reliable file position.
                    if not meta.get("track") and orig_meta.get("track"):
                        meta["track"] = orig_meta["track"]
                    if meta.get("disc") == 1 and orig_meta.get("disc", 1) > 1:
                        meta["disc"] = orig_meta["disc"]
                        meta["disctotal"] = (
                            orig_meta.get("disctotal") or meta["disctotal"])
                items.append((f, meta, "acoustid" if meta else "", receipt))
                time.sleep(ACOUSTID_RATE_DELAY)
        else:
            items.extend((f, None, "", receipt)
                         for f, _meta, receipt in unplaced)

        if not root_binding.matches_public():
            raise OSError("Migration source changed during the preview scan")
        return items
    finally:
        root_binding.close()


# ── Execution ─────────────────────────────────────────────────────────────────

def _is_resume_collision(entry: PlanEntry) -> bool:
    return (isinstance(entry, PlanEntry)
            and entry.status == COLLISION
            and entry.reason == "destination already exists"
            and entry.dest_rel is not None)


def _verified_existing_audio(plan: MigrationPlan, entry: PlanEntry, *,
                             cancel_check: Optional[Callable[[], bool]] = None
                             ) -> Optional[Path]:
    if (
        entry.source_receipt is None
        or entry.destination_receipt is None
        or entry.dest_rel is None
        or plan.source_root_receipt is None
        or plan.dest_root_receipt is None
    ):
        return None
    source_root = destination_root = None
    opened_source = opened_destination = None
    try:
        if cancel_check is not None and cancel_check():
            return None
        if (
            tuple(entry.source_receipt.get("relative") or ())
            != _relative_parts(
                entry.source, Path(plan.source_root_receipt["path"]))
            or tuple(entry.destination_receipt.get("relative") or ())
            != tuple(Path(entry.dest_rel).parts)
        ):
            return None
        source_root = _open_root_receipt(plan.source_root_receipt)
        destination_root = _open_root_receipt(plan.dest_root_receipt)
        opened_source = _open_file_receipt(
            source_root, entry.source_receipt)
        opened_destination = _open_file_receipt(
            destination_root, entry.destination_receipt)
        if cancel_check is not None and cancel_check():
            return None
        if (
            opened_source.verified_digest
            != opened_destination.verified_digest
            or not opened_source.still_exact(entry.source_receipt)
            or not opened_destination.still_exact(entry.destination_receipt)
        ):
            return None
        return Path(plan.dest_root) / Path(entry.dest_rel)
    except OSError:
        return None
    finally:
        if opened_destination is not None:
            opened_destination.close()
        if opened_source is not None:
            opened_source.close()
        if destination_root is not None:
            destination_root.close()
        if source_root is not None:
            source_root.close()


def verified_resume_entries(plan: MigrationPlan, *,
                            progress: Optional[Callable] = None,
                            cancel_check: Optional[Callable[[], bool]] = None
                            ) -> list:
    from qobuz_librarian.file_exclusion import inode_write_exclusion_scope

    with inode_write_exclusion_scope() as available:
        if not available:
            return []
        return _verified_resume_entries(
            plan, progress=progress, cancel_check=cancel_check)


def _verified_resume_entries(plan: MigrationPlan, *,
                             progress: Optional[Callable] = None,
                             cancel_check: Optional[Callable[[], bool]] = None
                             ) -> list:
    """Existing destinations currently proven identical to their sources."""
    candidates = [
        entry for entry in plan.collisions if _is_resume_collision(entry)
    ]
    verified = []
    for index, entry in enumerate(candidates, 1):
        if cancel_check is not None and cancel_check():
            break
        if progress:
            progress(
                "Checking existing copies", index, len(candidates),
                entry.source.name,
            )
        if cancel_check is not None and cancel_check():
            break
        if _verified_existing_audio(
                plan, entry,
                cancel_check=cancel_check) is not None:
            verified.append(entry)
        if cancel_check is not None and cancel_check():
            break
    return verified


def _open_destination_parent(binding: _RootBinding, dest_rel: Path,
                             path_receipt=None):
    parts = tuple(Path(dest_rel).parts)
    if (
        not parts
        or Path(dest_rel).is_absolute()
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise OSError("unsafe migration destination")
    existing = []
    missing = []
    if path_receipt is not None:
        try:
            if list(parts) != list(path_receipt["relative"]):
                raise OSError(
                    "migration destination proof does not match its path")
            existing = list(path_receipt["parents"])
            missing = list(path_receipt["missing_parents"])
            if (
                [item["name"] for item in existing] + missing
                != list(parts[:-1])
            ):
                raise OSError("migration destination proof is malformed")
        except (KeyError, TypeError, ValueError) as exc:
            raise OSError("migration destination proof is malformed") from exc
    descriptors = []
    parent_fd = binding.root_fd
    prefix = []
    try:
        for index, part in enumerate(parts[:-1]):
            prefix.append(part)
            if path_receipt is None:
                child_fd = _open_directory(part, dir_fd=parent_fd)
                if not binding.directory_is_trusted(prefix, child_fd):
                    os.close(child_fd)
                    raise OSError(
                        "migration destination parent was not preview-sealed")
            elif index < len(existing):
                child_fd = _open_directory(part, dir_fd=parent_fd)
                if (
                    _directory_identity(child_fd)
                    != _receipt_directory_identity(
                        existing[index].get("identity"))
                    or not binding.trust_directory(prefix, child_fd)
                ):
                    os.close(child_fd)
                    raise OSError(
                        "migration destination parent changed after preview")
            else:
                try:
                    child_fd = _open_directory(part, dir_fd=parent_fd)
                except FileNotFoundError:
                    child_fd = _create_directory_noreplace(parent_fd, part)
                    if not binding.trust_directory(prefix, child_fd):
                        os.close(child_fd)
                        raise OSError(
                            "migration destination directory proof conflicted")
                else:
                    if not binding.directory_is_trusted(prefix, child_fd):
                        os.close(child_fd)
                        raise OSError(
                            "migration destination parent appeared after preview")
            if not _named_entry_matches(parent_fd, part, child_fd):
                os.close(child_fd)
                raise OSError("migration destination parent changed")
            descriptors.append(child_fd)
            parent_fd = child_fd
        if not binding.matches_public():
            raise OSError("migration destination root changed")
        return descriptors, parent_fd, parts[-1]
    except BaseException:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _reserve_temporary_file(parent_fd):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("safe no-follow file creation is unavailable")
    for _ in range(16):
        name = f".qobuz-migrate-copy-{secrets.token_hex(16)}"
        try:
            descriptor = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=parent_fd,
            )
        except FileExistsError:
            continue
        if not _named_entry_matches(parent_fd, name, descriptor):
            os.close(descriptor)
            raise OSError("private migration file changed")
        return name, descriptor
    raise OSError("could not reserve a private migration file")


def _destination_chain_matches(binding, dest_rel, descriptors) -> bool:
    parts = tuple(Path(dest_rel).parts[:-1])
    if len(parts) != len(descriptors) or not binding.matches_public():
        return False
    prefix = []
    try:
        for parent_fd, name, child_fd in zip(
                [binding.root_fd] + descriptors, parts, descriptors):
            prefix.append(name)
            identity = _directory_identity(child_fd)
            if (
                binding.trusted_directories.get(tuple(prefix)) != identity
                or _named_directory_identity(parent_fd, name) != identity
            ):
                return False
        return True
    except (OSError, TypeError, ValueError):
        return False


def _published_file_still_exact(published) -> bool:
    if published is None:
        return False
    descriptor, parents, parent_fd, name, dest_rel, binding = published
    return (
        _destination_chain_matches(binding, dest_rel, parents)
        and _named_entry_matches(parent_fd, name, descriptor)
    )


def _remove_exact_named_file(parent_fd, name, descriptor) -> bool:
    if not _named_entry_matches(parent_fd, name, descriptor):
        return False
    try:
        os.unlink(name, dir_fd=parent_fd)
    except OSError:
        return False
    return _named_entry_missing(parent_fd, name)


def _remove_exact_named_file_privately(parent_fd, name, descriptor) -> bool:
    """Retire an exact file behind a fresh private namespace boundary.

    The public quarantine name is never passed to ``unlink``.  A late entry at
    that exposed name is instead restored by the exact no-overwrite handoff, or
    retained at a concrete recovery path when restoration is obstructed.
    """
    private_name = None
    private_fd = None
    moved = False

    def restore_exact() -> bool:
        nonlocal moved
        if private_fd is None:
            return False
        restored = _restore_private_exact(
            private_fd, "candidate", parent_fd, name, descriptor)
        if restored:
            moved = False
        return restored

    try:
        private_name, private_fd = _reserve_private_directory(
            parent_fd, prefix="qobuz-migrate-discard")
        deferred = _rename_exact_noreplace_at(
            parent_fd, name, private_fd, "candidate", descriptor)
        moved = _named_entry_matches(private_fd, "candidate", descriptor)
        if not moved:
            return False
        if deferred is not None:
            if not restore_exact():
                raise _preserved_entry_error(
                    private_fd,
                    "candidate",
                    "exact publication cleanup could not be restored",
                ) from deferred
            if isinstance(deferred, OSError):
                return False
            raise deferred
        try:
            _fsync_directories(parent_fd, private_fd)
        except BaseException:
            restore_exact()
            return False
        if not _remove_exact_named_file(
                private_fd, "candidate", descriptor):
            if not restore_exact():
                raise _preserved_entry_error(
                    private_fd,
                    "candidate",
                    "exact publication cleanup could not be restored",
                )
            return False
        moved = False
        try:
            _fsync_directories(parent_fd, private_fd)
        except BaseException:
            return False
        return True
    finally:
        preserved = None
        if moved:
            try:
                if not restore_exact():
                    if _named_entry_matches(
                            private_fd, "candidate", descriptor):
                        preserved = _preserved_entry_error(
                            private_fd,
                            "candidate",
                            "exact publication cleanup could not be restored",
                        )
                    else:
                        preserved = _MigrationEntryPreserved(
                            _descriptor_path(descriptor),
                            "exact publication cleanup changed unexpectedly",
                        )
            except _MigrationEntryPreserved as exc:
                preserved = exc
        if private_fd is not None:
            try:
                if (
                    not os.listdir(private_fd)
                    and private_name is not None
                    and _named_entry_matches(
                        parent_fd, private_name, private_fd)
                ):
                    os.rmdir(private_name, dir_fd=parent_fd)
                    if _named_entry_missing(parent_fd, private_name):
                        _fsync_directories(parent_fd)
            except BaseException:
                # Once the exact candidate is gone, an empty private directory
                # is harmless residue.  If it is still present, leave it alone.
                pass
            os.close(private_fd)
        if preserved is not None:
            raise preserved


def _file_fidelity_matches(descriptor, receipt, *, digest=None) -> bool:
    """Verify copied metadata, and optionally an already-computed digest."""
    try:
        value = os.fstat(descriptor)
        if (
            not stat.S_ISREG(value.st_mode)
            or int(value.st_mode) != int(receipt["mode"])
            or int(value.st_uid) != int(receipt["uid"])
            or int(value.st_gid) != int(receipt["gid"])
            or int(value.st_size) != int(receipt["size"])
            or int(value.st_atime_ns) != int(receipt["atime_ns"])
            or int(value.st_mtime_ns) != int(receipt["mtime_ns"])
            or _xattr_snapshot(descriptor) != receipt["xattrs"]
        ):
            return False
        return digest is None or digest == receipt["sha256"]
    except (KeyError, OSError, TypeError, ValueError):
        return False


class _PublishedCopyFailure(OSError):
    """Carry the exact same-run publication across an exceptional boundary."""

    def __init__(self, cause, published):
        message = str(cause) or type(cause).__name__
        error_number = getattr(cause, "errno", None) or errno.EIO
        super().__init__(error_number, message)
        self.cause = cause
        self.published = published
        self.interrupted = (
            isinstance(cause, KeyboardInterrupt)
            or getattr(cause, "errno", None) == errno.EINTR
        )


def _apply_file_metadata(destination_fd, source_receipt) -> None:
    """Copy and verify ownership, mode, timestamps, xattrs, and Linux ACLs."""
    expected = source_receipt["file"]
    expected_xattrs = _decode_xattr_snapshot(expected["xattrs"])
    current = os.fstat(destination_fd)
    if (
        int(current.st_uid) != int(expected["uid"])
        or int(current.st_gid) != int(expected["gid"])
    ):
        try:
            os.fchown(
                destination_fd,
                int(expected["uid"]),
                int(expected["gid"]),
            )
        except (AttributeError, OSError) as exc:
            current = os.fstat(destination_fd)
            if (
                int(current.st_uid) != int(expected["uid"])
                or int(current.st_gid) != int(expected["gid"])
            ):
                raise OSError(
                    "migration destination ownership could not be preserved"
                ) from exc
    os.fchmod(destination_fd, stat.S_IMODE(int(expected["mode"])))

    expected_names = {name for name, _value in expected_xattrs}
    for existing_name in os.listxattr(destination_fd):
        encoded = os.fsencode(existing_name)
        if encoded not in expected_names:
            os.removexattr(destination_fd, existing_name)
    for name, value in expected_xattrs:
        os.setxattr(destination_fd, os.fsdecode(name), value)

    os.utime(
        destination_fd,
        ns=(int(expected["atime_ns"]), int(expected["mtime_ns"])),
    )
    if not _file_fidelity_matches(
            destination_fd, source_receipt["file"]):
        raise OSError("migration destination metadata verification failed")


def _stream_copy_noreplace(source: _OpenedFile, source_receipt,
                           destination: _RootBinding, dest_rel: Path,
                           destination_path_receipt=None):
    """Copy one held source to an absent destination and keep its fd open."""
    parents, parent_fd, destination_name = _open_destination_parent(
        destination, dest_rel, destination_path_receipt)
    temporary_name = None
    temporary_fd = None
    published = False
    try:
        if not _named_entry_missing(parent_fd, destination_name):
            raise FileExistsError(
                errno.EEXIST, os.strerror(errno.EEXIST), destination_name)
        temporary_name, temporary_fd = _reserve_temporary_file(parent_fd)
        source_digest = source_receipt["file"]["sha256"]
        offset = 0
        while True:
            chunk = os.pread(source.descriptor, 1 << 20, offset)
            if not chunk:
                break
            written = 0
            while written < len(chunk):
                count = os.write(temporary_fd, chunk[written:])
                if count <= 0:
                    raise OSError("migration copy made no write progress")
                written += count
            offset += len(chunk)
        if (
            _digest_fd(temporary_fd) != source_digest
        ):
            raise OSError("migration destination bytes failed verification")
        _apply_file_metadata(temporary_fd, source_receipt)
        os.fsync(temporary_fd)
        if (
            not source.still_exact(source_receipt)
            or not _destination_chain_matches(
                destination, dest_rel, parents)
        ):
            raise OSError("migration source or destination changed during copy")
        try:
            _rename_noreplace_at(
                parent_fd, temporary_name, parent_fd, destination_name)
        except BaseException:
            published = _named_entry_matches(
                parent_fd, destination_name, temporary_fd)
            private = _named_entry_matches(
                parent_fd, temporary_name, temporary_fd)
            if private:
                _remove_exact_named_file(
                    parent_fd, temporary_name, temporary_fd)
            if published:
                try:
                    _fsync_directories(parent_fd)
                except OSError:
                    pass
            raise
        published = True
        if (
            not _named_entry_matches(
                parent_fd, destination_name, temporary_fd)
            or not _named_entry_missing(parent_fd, temporary_name)
            or not source.still_exact(source_receipt)
            or not _destination_chain_matches(
                destination, dest_rel, parents)
        ):
            raise OSError("migration publication could not be proven")
        _fsync_directories(parent_fd)

        # Do not carry the writable staging descriptor into source
        # retirement.  Reopen the published inode read-only so a Linux read
        # lease can exclude both existing and newly arriving writers while
        # the source's last link is retired.
        read_fd = _open_regular_file(destination_name, dir_fd=parent_fd)
        if (
            not _same_entry(os.fstat(read_fd), os.fstat(temporary_fd))
            or not _named_entry_matches(
                parent_fd, destination_name, read_fd)
            or not _destination_chain_matches(
                destination, dest_rel, parents)
        ):
            os.close(read_fd)
            raise OSError("migration publication changed after copy")
        writable_fd = temporary_fd
        temporary_fd = read_fd
        os.close(writable_fd)
        return (
            temporary_fd, parents, parent_fd, destination_name,
            Path(dest_rel), destination,
        )
    except BaseException as exc:
        if (
            published
            and temporary_fd is not None
            and _named_entry_matches(
                parent_fd, destination_name, temporary_fd)
        ):
            # An exception may land before the normal writable-to-read-only
            # descriptor handoff. Best-effort normalise here so rollback can
            # acquire a read lease; if this proof fails the writable descriptor
            # is retained and rollback will refuse rather than delete blindly.
            replacement_fd = None
            try:
                replacement_fd = _open_regular_file(
                    destination_name, dir_fd=parent_fd)
                if (
                    not _same_entry(
                        os.fstat(replacement_fd), os.fstat(temporary_fd))
                    or not _named_entry_matches(
                        parent_fd, destination_name, replacement_fd)
                ):
                    os.close(replacement_fd)
                    replacement_fd = None
                else:
                    previous_fd = temporary_fd
                    temporary_fd = replacement_fd
                    replacement_fd = None
                    os.close(previous_fd)
            except BaseException:
                if replacement_fd is not None:
                    try:
                        os.close(replacement_fd)
                    except OSError:
                        pass
            owned_publication = (
                temporary_fd,
                parents,
                parent_fd,
                destination_name,
                Path(dest_rel),
                destination,
            )
            temporary_fd = None
            parents = []
            raise _PublishedCopyFailure(
                exc, owned_publication) from exc
        if temporary_fd is not None:
            if (
                not published
                and temporary_name is not None
                and _named_entry_matches(
                    parent_fd, temporary_name, temporary_fd)
            ):
                _remove_exact_named_file(parent_fd, temporary_name, temporary_fd)
            try:
                os.close(temporary_fd)
            except OSError:
                pass
        for descriptor in reversed(parents):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _discard_owned_publication(
        published, expected_receipt, *, recoveries=None) -> bool:
    """Remove only the exact destination inode this invocation published."""
    if not _published_file_still_exact(published):
        return False
    descriptor, _parents, parent_fd, name, _dest_rel, _binding = published
    from qobuz_librarian.file_exclusion import acquire_inode_write_exclusion

    exclusion = acquire_inode_write_exclusion(descriptor)
    if exclusion is None:
        return False
    quarantine_name = None
    quarantine_fd = None
    moved = False

    def restore_exact() -> bool:
        nonlocal moved
        if quarantine_fd is None:
            return False
        restored = _restore_private_exact(
            quarantine_fd, "held", parent_fd, name, descriptor)
        if restored:
            moved = False
        return restored

    def clear_quarantine() -> bool:
        nonlocal quarantine_name
        if quarantine_name is None or quarantine_fd is None:
            return True
        try:
            if os.listdir(quarantine_fd):
                return False
            if not _named_entry_matches(
                    parent_fd, quarantine_name, quarantine_fd):
                return False
            os.rmdir(quarantine_name, dir_fd=parent_fd)
            if not _named_entry_missing(parent_fd, quarantine_name):
                return False
            quarantine_name = None
            _fsync_directories(parent_fd)
            return True
        except BaseException:
            return False

    def record_preserved_replacement(exc) -> None:
        if recoveries is None:
            return
        _descriptor, _parents, _parent_fd, _name, dest_rel, binding = published
        location = Path(exc.location)
        try:
            relative = location.relative_to(binding.path)
        except ValueError:
            relative = location
        record = {
            "state": "replacement-guarded",
            "location": os.fspath(location),
            "relative": os.fspath(relative),
            "destination": os.fspath(Path(dest_rel)),
            "reason": (
                "a second destination arrival prevented safe restoration "
                "of the first replacement"
            ),
            "restart": (
                "Keep both files. Move the recorded recovery file only after "
                "reviewing the occupied destination path."
            ),
        }
        if record not in recoveries:
            recoveries.append(record)

    try:
        verified_digest = _digest_fd(descriptor)
        if (
            not exclusion.intact()
            or not _file_fidelity_matches(
                descriptor,
                expected_receipt,
                digest=verified_digest,
            )
            or not _published_file_still_exact(published)
        ):
            return False
        try:
            quarantine_name, quarantine_fd = _reserve_private_directory(
                parent_fd, prefix="qobuz-migrate-publication")
            deferred = _rename_exact_noreplace_at(
                parent_fd, name, quarantine_fd, "held", descriptor)
        except _MigrationEntryPreserved as exc:
            record_preserved_replacement(exc)
            return False
        except (OSError, TypeError, ValueError):
            return False
        moved = _named_entry_matches(quarantine_fd, "held", descriptor)
        if not moved:
            return False
        if (
            not _named_entry_missing(parent_fd, name)
            or not exclusion.intact()
            or not _file_fidelity_matches(
                descriptor,
                expected_receipt,
                digest=verified_digest,
            )
            or not _destination_chain_matches(
                published[5], published[4], published[1])
        ):
            restore_exact()
            return False
        if deferred is not None and not isinstance(deferred, OSError):
            restore_exact()
            raise deferred
        try:
            _fsync_directories(parent_fd, quarantine_fd)
        except BaseException:
            restore_exact()
            return False
        try:
            removed = _remove_exact_named_file_privately(
                quarantine_fd, "held", descriptor)
        except _MigrationEntryPreserved as exc:
            record_preserved_replacement(exc)
            return False
        if not removed:
            restore_exact()
            return False
        moved = False
        try:
            _fsync_directories(parent_fd, quarantine_fd)
        except BaseException:
            return False
        if not clear_quarantine():
            return False
        return (
            exclusion.intact()
            and not _named_entry_matches(parent_fd, name, descriptor)
        )
    finally:
        if moved:
            restore_exact()
        clear_quarantine()
        if quarantine_fd is not None:
            try:
                os.close(quarantine_fd)
            except OSError:
                pass
        exclusion.close()


def _owned_publication_recovery(published, source, source_receipt,
                                reason) -> dict:
    descriptor, _parents, _parent_fd, _name, dest_rel, binding = published
    try:
        location = _descriptor_path(descriptor)
    except OSError:
        location = binding.path / Path(dest_rel)
    return {
        "state": "duplicate-publication",
        "location": os.fspath(location),
        "relative": os.fspath(Path(dest_rel)),
        "destination": os.fspath(Path(dest_rel)),
        "source": os.fspath(Path(source)),
        "file_receipt": source_receipt.get("file"),
        "reason": reason,
        "restart": (
            "This exact destination was created by the interrupted migration "
            "while the source was retained. Do not delete either file until "
            "the recorded source and destination have been reviewed."
        ),
    }


def _reserve_private_directory(parent_fd, *, prefix):
    for _ in range(16):
        name = f".{prefix}-{secrets.token_hex(16)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        descriptor = _open_directory(name, dir_fd=parent_fd)
        if not _named_entry_matches(parent_fd, name, descriptor):
            os.close(descriptor)
            raise OSError("private source quarantine changed")
        return name, descriptor
    raise OSError("could not reserve a private migration directory")


def _restore_private_exact(private_fd, private_name, public_fd, public_name,
                           expected_fd) -> bool:
    """Restore one held exact entry without allowing an overwrite.

    The no-replace rename is an uncertain commit boundary.  Inspect both names
    after every exceptional exit so an interrupt that landed the rename still
    counts as a successful rollback.
    """
    if (
        _named_entry_matches(public_fd, public_name, expected_fd)
        and _named_entry_missing(private_fd, private_name)
    ):
        return True
    if (
        not _named_entry_missing(public_fd, public_name)
        or not _named_entry_matches(private_fd, private_name, expected_fd)
    ):
        return False
    try:
        _rename_noreplace_at(
            private_fd, private_name, public_fd, public_name)
    except BaseException:
        pass
    return (
        _named_entry_matches(public_fd, public_name, expected_fd)
        and _named_entry_missing(private_fd, private_name)
    )


class _DestinationSafety:
    """Keep the exact durable destination recoverable during retirement."""

    def __init__(self, published, expected_receipt, verified_digest,
                 private_name, private_fd, exclusion):
        self.published = published
        self.expected_receipt = expected_receipt
        self.verified_digest = verified_digest
        self.private_name = private_name
        self.private_fd = private_fd
        self.exclusion = exclusion
        self.released = False
        self.release_attempted = False
        self.recovery = None
        self.interrupted = False

    def _note_exception(self, exc) -> None:
        if (
            not isinstance(exc, OSError)
            or getattr(exc, "errno", None) == errno.EINTR
        ):
            self.interrupted = True

    @property
    def descriptor(self):
        return self.published[0]

    @property
    def parent_fd(self):
        return self.published[2]

    @property
    def destination_name(self):
        return self.published[3]

    def _chain_exact(self) -> bool:
        _descriptor, parents, _parent_fd, _name, dest_rel, binding = \
            self.published
        return _destination_chain_matches(binding, dest_rel, parents)

    def _private_directory_exact(self) -> bool:
        return _named_entry_matches(
            self.parent_fd, self.private_name, self.private_fd)

    def _anchor_exact(self) -> bool:
        return _named_entry_matches(
            self.private_fd, "anchor", self.descriptor)

    def _recovery_exact(self) -> bool:
        return _named_entry_matches(
            self.private_fd, "recovery", self.descriptor)

    def _fidelity_exact(self) -> bool:
        return (
            self.exclusion is not None
            and self.exclusion.intact()
            and _file_fidelity_matches(
                self.descriptor,
                self.expected_receipt,
                digest=self.verified_digest,
            )
        )

    def _record_recovery(self, state, reason) -> None:
        _descriptor, _parents, _parent_fd, _name, dest_rel, binding = \
            self.published
        if self._recovery_exact():
            private_link = "recovery"
        elif self._anchor_exact():
            private_link = "anchor"
        else:
            return
        relative = Path(dest_rel).parent / self.private_name / private_link
        try:
            location = _descriptor_path(self.private_fd) / private_link
        except OSError:
            location = binding.path / relative
        self.recovery = {
            "state": state,
            "location": os.fspath(location),
            "relative": os.fspath(relative),
            "destination": os.fspath(Path(dest_rel)),
            "reason": reason,
            "restart": (
                "Do not delete the recovery file. Re-run migration after "
                "restoring the recorded destination path, or move this exact "
                "file there only when the destination name is absent."
            ),
        }

    def intact(self) -> bool:
        return (
            not self.released
            and self.exclusion is not None
            and self.exclusion.intact()
            and self._private_directory_exact()
            and self._anchor_exact()
            and self._recovery_exact()
            and _published_file_still_exact(self.published)
            and self._fidelity_exact()
        )

    def _remove_empty_private_directory(self) -> None:
        if not self._private_directory_exact():
            return
        try:
            if os.listdir(self.private_fd):
                return
            os.rmdir(self.private_name, dir_fd=self.parent_fd)
            if _named_entry_missing(self.parent_fd, self.private_name):
                _fsync_directories(self.parent_fd)
        except BaseException as exc:
            self._note_exception(exc)
            # Once an exact public destination is proved, an empty private
            # directory is harmless residue.  Never broaden cleanup after an
            # uncertain interruption.
            pass

    def release(self) -> bool:
        """Restore a missing public name or drop the exact private guard."""
        if self.released:
            return True
        self.release_attempted = True
        if (
            self.exclusion is None
            or not self.exclusion.intact()
            or not self._private_directory_exact()
        ):
            if self._recovery_exact() or self._anchor_exact():
                self._record_recovery(
                    "guarded",
                    "destination safety could not be reverified",
                )
            return False
        if not self._fidelity_exact():
            try:
                _apply_file_metadata(
                    self.descriptor,
                    {"file": self.expected_receipt},
                )
                os.fsync(self.descriptor)
            except BaseException as exc:
                self._note_exception(exc)
            if not self._fidelity_exact():
                if self._recovery_exact() or self._anchor_exact():
                    self._record_recovery(
                        "guarded",
                        "destination metadata could not be restored",
                    )
                return False

        public_exact = (
            self._chain_exact()
            and _named_entry_matches(
                self.parent_fd, self.destination_name, self.descriptor)
        )
        anchor_exact = self._anchor_exact()
        recovery_exact = self._recovery_exact()
        if not public_exact:
            if (
                not self._chain_exact()
                or not _named_entry_missing(
                    self.parent_fd, self.destination_name)
                or not anchor_exact
                or not recovery_exact
                or not _restore_private_exact(
                    self.private_fd,
                    "anchor",
                    self.parent_fd,
                    self.destination_name,
                    self.descriptor,
                )
            ):
                # A replacement or changed parent is never overwritten.  The
                # exact bytes remain under the private random safety name.
                if recovery_exact or anchor_exact:
                    self._record_recovery(
                        "sole-copy-possible",
                        "public destination could not be restored safely",
                    )
                return False
            try:
                _fsync_directories(self.parent_fd, self.private_fd)
            except BaseException as exc:
                self._note_exception(exc)
                if self._recovery_exact() or self._anchor_exact():
                    self._record_recovery(
                        "guarded",
                        "restored destination could not be flushed",
                    )
                return False
            public_exact = _published_file_still_exact(self.published)
            anchor_exact = self._anchor_exact()
            recovery_exact = self._recovery_exact()

        if not public_exact or not recovery_exact or not self._fidelity_exact():
            if recovery_exact or anchor_exact:
                self._record_recovery(
                    "sole-copy-possible",
                    "public destination disappeared during recovery",
                )
            return False
        if anchor_exact:
            interrupted = None
            try:
                os.unlink("anchor", dir_fd=self.private_fd)
            except BaseException as exc:
                interrupted = exc
                self._note_exception(exc)
            if not _named_entry_missing(self.private_fd, "anchor"):
                if self._recovery_exact() or self._anchor_exact():
                    self._record_recovery(
                        "redundant-guard",
                        "private destination guard cleanup was interrupted",
                    )
                return False
            try:
                _fsync_directories(self.private_fd)
            except BaseException as exc:
                self._note_exception(exc)
                if self._recovery_exact() or self._anchor_exact():
                    self._record_recovery(
                        "guarded",
                        "private destination guard cleanup could not be "
                        "flushed",
                    )
                return False
            if interrupted is not None:
                if not isinstance(interrupted, OSError):
                    if self._recovery_exact() or self._anchor_exact():
                        self._record_recovery(
                            "redundant-guard",
                            "public destination is complete; private guard "
                            "cleanup can be retried",
                        )
                    return False

        # The independent recovery link remains throughout the anchor cleanup.
        # This is the coherent completion proof: exact durable public bytes and
        # an exact durable private recovery link are simultaneously present.
        if (
            not _published_file_still_exact(self.published)
            or not self._recovery_exact()
            or not self._fidelity_exact()
        ):
            if self._recovery_exact() or self._anchor_exact():
                self._record_recovery(
                    "sole-copy-possible",
                    "public destination disappeared before completion",
                )
            return False
        try:
            _fsync_directories(self.parent_fd, self.private_fd)
        except BaseException as exc:
            self._note_exception(exc)
            self._record_recovery(
                "guarded", "completion proof could not be flushed")
            return False

        if not self._fidelity_exact():
            self._record_recovery(
                "guarded", "destination metadata changed before completion")
            return False

        try:
            os.unlink("recovery", dir_fd=self.private_fd)
        except BaseException as exc:
            self._note_exception(exc)
        if not _named_entry_missing(self.private_fd, "recovery"):
            if self._recovery_exact():
                self._record_recovery(
                    "redundant-guard",
                    "public destination is complete; private guard cleanup "
                    "can be retried",
                )
                self.released = True
                return True
            return False
        # The final fidelity and lease proof immediately above authorises
        # removing the redundant link. The still-held read lease prevents a
        # writer from changing the public inode across this unlink boundary.
        self.released = True
        try:
            _fsync_directories(self.private_fd)
        except BaseException as exc:
            self._note_exception(exc)
            pass
        self._remove_empty_private_directory()
        return True

    def close(self) -> None:
        if not self.released and not self.release_attempted:
            try:
                self.release()
            except BaseException:
                pass
        if self.exclusion is not None:
            self.exclusion.close()
            self.exclusion = None
        try:
            os.close(self.private_fd)
        except OSError:
            pass


def _hold_published_destination(published, expected_receipt):
    """Seal a random exact safety link before destructive source work."""
    if not _published_file_still_exact(published):
        raise OSError("migration destination changed before source retirement")
    descriptor, _parents, parent_fd, destination_name, _dest_rel, _binding = \
        published
    from qobuz_librarian.file_exclusion import acquire_inode_write_exclusion

    exclusion = acquire_inode_write_exclusion(descriptor)
    if exclusion is None:
        raise OSError("migration destination could not be protected from writers")
    private_name = None
    private_fd = None
    try:
        verified_digest = _digest_fd(descriptor)
        if (
            not exclusion.intact()
            or not _file_fidelity_matches(
                descriptor,
                expected_receipt,
                digest=verified_digest,
            )
            or not _published_file_still_exact(published)
        ):
            raise OSError("migration destination changed before source retirement")
        private_name, private_fd = _reserve_private_directory(
            parent_fd, prefix="qobuz-migrate-destination")
        try:
            os.link(
                destination_name,
                "anchor",
                src_dir_fd=parent_fd,
                dst_dir_fd=private_fd,
                follow_symlinks=False,
            )
            os.link(
                destination_name,
                "recovery",
                src_dir_fd=parent_fd,
                dst_dir_fd=private_fd,
                follow_symlinks=False,
            )
        except BaseException:
            for private_link in ("recovery", "anchor"):
                if _named_entry_matches(
                        private_fd, private_link, descriptor):
                    try:
                        os.unlink(private_link, dir_fd=private_fd)
                    except BaseException:
                        pass
            raise
        safety = _DestinationSafety(
            published,
            expected_receipt,
            verified_digest,
            private_name,
            private_fd,
            exclusion,
        )
        if not safety.intact():
            raise OSError("migration destination changed while it was sealed")
        _fsync_directories(parent_fd, private_fd)
        if not safety.intact():
            raise OSError("migration destination changed while it was sealed")
        private_fd = None
        exclusion = None
        return safety
    finally:
        if private_fd is not None:
            for private_link in ("recovery", "anchor"):
                if _named_entry_matches(
                        private_fd, private_link, descriptor):
                    try:
                        os.unlink(private_link, dir_fd=private_fd)
                    except BaseException:
                        pass
            try:
                if (
                    private_name is not None
                    and not os.listdir(private_fd)
                    and _named_entry_matches(
                        parent_fd, private_name, private_fd)
                ):
                    os.rmdir(private_name, dir_fd=parent_fd)
                    _fsync_directories(parent_fd)
            except BaseException:
                pass
            try:
                os.close(private_fd)
            except OSError:
                pass
        if exclusion is not None:
            exclusion.close()


def _remove_sealed_source(source: _OpenedFile, receipt,
                          destination_intact=None, recoveries=None) -> bool:
    """Remove only the exact reviewed source after its destination is durable."""
    if not source.still_exact(receipt):
        return False
    quarantine_name = None
    quarantine_fd = None
    anchor_fd = None
    moved_exact = False
    committed = False

    def _require_destination() -> None:
        if destination_intact is not None and not destination_intact():
            raise OSError(
                "migration destination changed during source retirement")

    def _require_private_source_state() -> None:
        if (
            not source.parent_chain_exact()
            or not _named_entry_missing(source.parent_fd, source.name)
            or source.exclusion is None
            or not source.exclusion.intact()
            or not _anchor_exact()
            or not _file_receipt_matches(
                source.descriptor,
                receipt["file"],
                digest=source.verified_digest,
                allow_changed_time=True,
            )
        ):
            raise OSError(
                "migration source changed during source retirement")

    def _public_exact() -> bool:
        return _named_entry_matches(
            source.parent_fd, source.name, source.descriptor)

    def _held_exact() -> bool:
        return (
            quarantine_fd is not None
            and _named_entry_matches(
                quarantine_fd, "held", source.descriptor)
        )

    def _anchor_exact() -> bool:
        return (
            quarantine_fd is not None
            and anchor_fd is not None
            and _named_entry_matches(quarantine_fd, "anchor", anchor_fd)
        )

    def _restore_exact() -> bool:
        if _public_exact():
            return True
        if _held_exact() and _restore_private_exact(
                quarantine_fd, "held", source.parent_fd, source.name,
                source.descriptor):
            return True
        if _anchor_exact() and _restore_private_exact(
                quarantine_fd, "anchor", source.parent_fd, source.name,
                anchor_fd):
            return True
        return _public_exact()

    def _clear_private_link(name, descriptor) -> None:
        if descriptor is None or quarantine_fd is None:
            return
        try:
            _remove_exact_named_file(quarantine_fd, name, descriptor)
        except BaseException:
            pass

    def _clear_empty_quarantine() -> None:
        nonlocal quarantine_name
        if quarantine_name is None or quarantine_fd is None:
            return
        try:
            if (
                not os.listdir(quarantine_fd)
                and _named_entry_matches(
                    source.parent_fd, quarantine_name, quarantine_fd)
            ):
                os.rmdir(quarantine_name, dir_fd=source.parent_fd)
                if _named_entry_missing(source.parent_fd, quarantine_name):
                    quarantine_name = None
                _fsync_directories(source.parent_fd)
        except BaseException:
            pass

    def _record_source_recovery(reason) -> None:
        if recoveries is None or quarantine_name is None:
            return
        if _held_exact():
            private_link = "held"
        elif _anchor_exact():
            private_link = "anchor"
        else:
            return
        original_relative = Path(*receipt["relative"])
        recovery_relative = Path(
            *receipt["relative"][:-1],
            quarantine_name,
            private_link,
        )
        try:
            location = _descriptor_path(quarantine_fd) / private_link
        except OSError:
            location = source.root.path / recovery_relative
        record = {
            "state": "source-guarded",
            "location": os.fspath(location),
            "relative": os.fspath(recovery_relative),
            "destination": os.fspath(original_relative),
            "reason": reason,
            "restart": (
                "Do not delete the recorded recovery file. Restore it to "
                "the recorded source path only when that name is absent, "
                "then re-run migration."
            ),
        }
        if record not in recoveries:
            recoveries.append(record)
        log.error(
            "Migration retained exact source recovery at %s",
            record["location"],
        )

    try:
        _require_destination()
        quarantine_name, quarantine_fd = _reserve_private_directory(
            source.parent_fd, prefix="qobuz-migrate-source")
        try:
            os.link(
                source.name,
                "anchor",
                src_dir_fd=source.parent_fd,
                dst_dir_fd=quarantine_fd,
                follow_symlinks=False,
            )
        except BaseException:
            # ``link`` may have committed before an injected interrupt.  An
            # exact app-created link is safe to remove; anything ambiguous is
            # retained in the private directory.
            try:
                anchor_fd = _open_regular_file(
                    "anchor", dir_fd=quarantine_fd)
            except OSError:
                anchor_fd = None
            if (
                anchor_fd is not None
                and _same_entry(
                    os.fstat(anchor_fd), os.fstat(source.descriptor))
            ):
                _clear_private_link("anchor", anchor_fd)
            _clear_empty_quarantine()
            raise
        anchor_fd = _open_regular_file("anchor", dir_fd=quarantine_fd)
        if (
            not _same_entry(
                os.fstat(anchor_fd), os.fstat(source.descriptor))
            or not source.namespace_exact()
            or source.exclusion is None
            or not source.exclusion.intact()
            or not _file_receipt_matches(
                source.descriptor, receipt["file"],
                digest=source.verified_digest,
                allow_changed_time=True)
        ):
            raise OSError("migration source changed before retirement")
        _require_destination()
        try:
            _rename_noreplace_at(
                source.parent_fd, source.name, quarantine_fd, "held")
        except BaseException:
            moved_exact = (
                _named_entry_missing(source.parent_fd, source.name)
                and _named_entry_matches(
                    quarantine_fd, "held", source.descriptor)
            )
            if moved_exact:
                _restore_exact()
                moved_exact = False
            elif (
                _named_entry_missing(source.parent_fd, source.name)
                and not _named_entry_missing(quarantine_fd, "held")
            ):
                held_fd = None
                try:
                    held_fd = _open_entry_handle(
                        "held", dir_fd=quarantine_fd)
                    _restore_private_exact(
                        quarantine_fd, "held",
                        source.parent_fd, source.name, held_fd)
                except OSError:
                    pass
                finally:
                    if held_fd is not None:
                        os.close(held_fd)
            raise
        moved_exact = (
            _named_entry_missing(source.parent_fd, source.name)
            and _named_entry_matches(quarantine_fd, "held", source.descriptor)
        )
        if not moved_exact:
            # A same-name replacement may have been moved into the private
            # directory.  Put that unowned object back and preserve our anchor.
            if (
                not _named_entry_missing(quarantine_fd, "held")
                and _named_entry_missing(source.parent_fd, source.name)
            ):
                held_fd = _open_entry_handle("held", dir_fd=quarantine_fd)
                try:
                    _restore_private_exact(
                        quarantine_fd, "held",
                        source.parent_fd, source.name, held_fd)
                finally:
                    os.close(held_fd)
            raise OSError("migration source changed during retirement")
        _require_private_source_state()
        try:
            _fsync_directories(source.parent_fd, quarantine_fd)
        except BaseException:
            _restore_exact()
            moved_exact = False
            raise

        _require_private_source_state()
        _require_destination()

        # Retire the held public link while the exact private safety link keeps
        # the bytes recoverable.  If the unlink is interrupted after commit,
        # roll the anchor back to the absent public name.
        try:
            os.unlink("held", dir_fd=quarantine_fd)
        except BaseException:
            if _held_exact():
                _restore_exact()
            elif _named_entry_missing(quarantine_fd, "held"):
                _restore_exact()
            moved_exact = False
            raise
        moved_exact = False
        if (
            not _named_entry_missing(quarantine_fd, "held")
            or not _anchor_exact()
        ):
            _restore_exact()
            raise OSError("exact migration source retirement was ambiguous")

        _require_private_source_state()
        _require_destination()

        # This unlink is the sole forward commit: the destination is already
        # data-fsynced and namespace-fsynced, and ``source.descriptor`` still
        # identifies the reviewed inode while the syscall is reconciled.
        try:
            os.unlink("anchor", dir_fd=quarantine_fd)
        except BaseException:
            if _anchor_exact():
                _restore_exact()
                raise
            if not _named_entry_missing(quarantine_fd, "anchor"):
                raise
            committed = True
        else:
            if not _named_entry_missing(quarantine_fd, "anchor"):
                _restore_exact()
                raise OSError("migration source commit was ambiguous")
            committed = True

        # Once the final exact link is gone, a post-commit flush or cleanup
        # error cannot be rolled back.  The durable destination is the coherent
        # forward state, so classify the item as moved and leave only possible
        # app-owned empty residue.
        try:
            _fsync_directories(source.parent_fd, quarantine_fd)
        except BaseException:
            pass
        _clear_empty_quarantine()
        return True
    except BaseException:
        if not committed:
            if moved_exact or not _public_exact():
                _restore_exact()
            if _public_exact():
                _clear_private_link("held", source.descriptor)
                _clear_private_link("anchor", anchor_fd)
                _clear_empty_quarantine()
            if not _public_exact():
                _record_source_recovery(
                    "exact source could not be restored without overwrite")
            elif _held_exact() or _anchor_exact():
                _record_source_recovery(
                    "redundant private source guard could not be removed")
        raise
    finally:
        if anchor_fd is not None:
            try:
                os.close(anchor_fd)
            except OSError:
                pass
        if quarantine_fd is not None:
            try:
                os.close(quarantine_fd)
            except OSError:
                pass


def _open_sealed_directory_chain(binding, chain):
    if not isinstance(chain, list) or not chain:
        raise OSError("migration source directory receipt is malformed")
    descriptors = []
    parent_fd = binding.root_fd
    try:
        for item in chain:
            name = item["name"]
            expected = _receipt_directory_identity(item.get("identity"))
            if not _safe_component(name):
                raise OSError(
                    "migration source directory receipt is malformed")
            child_fd = _open_directory(name, dir_fd=parent_fd)
            if (
                not _named_entry_matches(parent_fd, name, child_fd)
                or _directory_identity(child_fd) != expected
                or not binding.directory_is_on_root_mount(child_fd)
            ):
                os.close(child_fd)
                raise OSError(
                    "migration source directory changed after preview")
            descriptors.append(child_fd)
            parent_fd = child_fd
        if not binding.matches_public():
            raise OSError("migration source root changed after preview")
        return descriptors
    except BaseException:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _sealed_directory_chain_matches(binding, chain, descriptors) -> bool:
    if len(chain) != len(descriptors) or not binding.matches_public():
        return False
    try:
        for parent_fd, item, child_fd in zip(
                [binding.root_fd] + descriptors, chain, descriptors):
            expected = _receipt_directory_identity(item.get("identity"))
            if (
                expected is None
                or _directory_identity(child_fd) != expected
                or not binding.directory_is_on_root_mount(child_fd)
                or _named_directory_identity(
                    parent_fd, item["name"]) != expected
            ):
                return False
        return True
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _remove_empty_sealed_directory(binding, chain, *, recoveries=None) -> bool:
    """Remove one exact preview-sealed empty directory, or preserve it."""
    descriptors = _open_sealed_directory_chain(binding, chain)
    target_fd = descriptors[-1]
    parent_fd = binding.root_fd if len(descriptors) == 1 else descriptors[-2]
    name = chain[-1]["name"]
    quarantine_name = None
    quarantine_fd = None
    committed = False

    def _restore_held(expected_fd) -> bool:
        return _restore_private_exact(
            quarantine_fd, "held", parent_fd, name, expected_fd)

    def _restore_any_held() -> bool:
        held_fd = None
        try:
            held_fd = _open_entry_handle("held", dir_fd=quarantine_fd)
            return _restore_held(held_fd)
        except OSError:
            return False
        finally:
            if held_fd is not None:
                os.close(held_fd)

    def _record_held_recovery(reason) -> None:
        if (
            recoveries is None
            or quarantine_name is None
            or quarantine_fd is None
            or not _named_entry_matches(
                quarantine_fd, "held", target_fd)
        ):
            return
        original_relative = Path(
            *(item["name"] for item in chain))
        recovery_relative = Path(
            *(item["name"] for item in chain[:-1]),
            quarantine_name,
            "held",
        )
        try:
            location = _descriptor_path(quarantine_fd) / "held"
        except OSError:
            location = binding.path / recovery_relative
        record = {
            "state": "directory-guarded",
            "location": os.fspath(location),
            "relative": os.fspath(recovery_relative),
            "destination": os.fspath(original_relative),
            "reason": reason,
            "restart": (
                "Do not delete the recorded recovery directory. Restore it "
                "to the recorded source path only when that path is absent, "
                "then re-run migration."
            ),
        }
        if record not in recoveries:
            recoveries.append(record)

    def _restore_or_record(reason) -> bool:
        if _restore_held(target_fd):
            return True
        _record_held_recovery(reason)
        return False

    try:
        if os.listdir(target_fd):
            return False
        if not _sealed_directory_chain_matches(binding, chain, descriptors):
            return False
        quarantine_name, quarantine_fd = _reserve_private_directory(
            parent_fd, prefix="qobuz-migrate-empty")
        try:
            _rename_noreplace_at(
                parent_fd, name, quarantine_fd, "held")
        except BaseException:
            if _named_entry_matches(quarantine_fd, "held", target_fd):
                _restore_or_record(
                    "source directory rename could not be reconciled")
            elif (
                _named_entry_missing(parent_fd, name)
                and not _named_entry_missing(quarantine_fd, "held")
            ):
                _restore_any_held()
            raise
        if (
            not _named_entry_missing(parent_fd, name)
            or not _named_entry_matches(
                quarantine_fd, "held", target_fd)
        ):
            if _named_entry_missing(parent_fd, name):
                if not _restore_any_held():
                    _record_held_recovery(
                        "source directory state became ambiguous")
            raise OSError("migration source directory changed during cleanup")
        try:
            changed = bool(os.listdir(target_fd))
        except BaseException as exc:
            if not _restore_or_record(
                    "source directory could not be inspected after rename"):
                if isinstance(exc, OSError):
                    return False
                raise
            if isinstance(exc, OSError):
                return False
            raise
        if changed:
            if not _restore_or_record(
                    "source directory changed after it was renamed"):
                raise OSError(
                    "changed migration directory could not be restored")
            return False
        try:
            _fsync_directories(parent_fd, quarantine_fd)
            os.rmdir("held", dir_fd=quarantine_fd)
        except BaseException as exc:
            committed = _named_entry_missing(quarantine_fd, "held")
            if committed:
                pass
            elif _named_entry_matches(quarantine_fd, "held", target_fd):
                if not _restore_or_record(
                        "empty source directory could not be restored"):
                    raise OSError(
                        "empty migration directory could not be restored"
                    ) from exc
                if isinstance(exc, OSError):
                    return False
                raise
            else:
                raise OSError(
                    "empty migration directory cleanup was ambiguous"
                ) from exc
        else:
            committed = _named_entry_missing(quarantine_fd, "held")
            if not committed:
                if not _restore_or_record(
                        "empty source directory removal was not committed"):
                    raise OSError(
                        "empty migration directory could not be restored")
                return False
        try:
            _fsync_directories(parent_fd, quarantine_fd)
        except BaseException:
            pass
        return True
    finally:
        if quarantine_fd is not None:
            if (
                not committed
                and _named_entry_matches(
                    quarantine_fd, "held", target_fd)
            ):
                _record_held_recovery(
                    "source directory cleanup ended before restoration")
            if quarantine_name is not None:
                try:
                    if (
                        not os.listdir(quarantine_fd)
                        and _named_entry_matches(
                            parent_fd, quarantine_name, quarantine_fd)
                    ):
                        os.rmdir(quarantine_name, dir_fd=parent_fd)
                        _fsync_directories(parent_fd)
                except BaseException:
                    pass
            try:
                os.close(quarantine_fd)
            except OSError:
                pass
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _prune_retired_source_parents(binding, receipts, *, recoveries=None,
                                  result=None, cancel_check=None) -> int:
    candidates = {}
    for receipt in receipts:
        try:
            parents = list(receipt["parents"])
        except (KeyError, TypeError):
            continue
        for depth in range(1, len(parents) + 1):
            chain = parents[:depth]
            try:
                key = tuple(
                    (item["name"], tuple(item["identity"]))
                    for item in chain
                )
            except (KeyError, TypeError):
                continue
            candidates[key] = chain
    removed = 0
    for chain in sorted(candidates.values(), key=len, reverse=True):
        if cancel_check is not None and cancel_check():
            if result is not None:
                result.cancelled = True
            break
        try:
            if _remove_empty_sealed_directory(
                    binding, chain, recoveries=recoveries):
                removed += 1
                if result is not None:
                    result.pruned += 1
        except OSError:
            continue
    return removed


def _close_published_file(published) -> None:
    if published is None:
        return
    descriptor, parents, _parent_fd, _name, _dest_rel, _binding = published
    try:
        os.close(descriptor)
    except OSError:
        pass
    for parent in reversed(parents):
        try:
            os.close(parent)
        except OSError:
            pass


def _path_evidence(path) -> str:
    return base64.b64encode(os.fsencode(path)).decode("ascii")


def _canonical_digest(value) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _entry_evidence(entry: PlanEntry) -> dict:
    return {
        "source": _path_evidence(entry.source),
        "status": entry.status,
        "destination": (
            _path_evidence(entry.dest_rel)
            if entry.dest_rel is not None else None
        ),
        "source_receipt": entry.source_receipt,
        "destination_receipt": entry.destination_receipt,
        "destination_path_receipt": entry.destination_path_receipt,
    }


def _entry_seal(entry: PlanEntry) -> str:
    return _canonical_digest(_entry_evidence(entry))


def _plan_context(plan: MigrationPlan) -> dict:
    return {
        "source_root": (
            _path_evidence(plan.source_root)
            if plan.source_root is not None else None
        ),
        "destination_root": _path_evidence(plan.dest_root),
        "source_root_receipt": plan.source_root_receipt,
        "destination_root_receipt": plan.dest_root_receipt,
        "destination_name_semantics": plan.destination_name_semantics,
        "companion_receipts": plan.companion_receipts,
    }


def _validate_root_receipt(receipt, expected_path) -> None:
    if not isinstance(receipt, dict):
        raise OSError("migration root receipt is missing")
    try:
        if (
            not isinstance(receipt["path"], str)
            or not isinstance(receipt["anchor"], str)
            or not isinstance(receipt["existing"], list)
            or not isinstance(receipt["missing"], list)
        ):
            raise OSError("migration root receipt is malformed")
        absolute, anchor, parts = _absolute_parts(Path(receipt["path"]))
        existing = receipt["existing"]
        missing = receipt["missing"]
        if (
            absolute != _lexical_absolute(Path(expected_path))
            or receipt["anchor"] != os.fspath(anchor)
            or _receipt_directory_identity(receipt["anchor_identity"]) is None
            or any(not isinstance(item, dict) for item in existing)
            or any(not _safe_component(name) for name in missing)
            or [item["name"] for item in existing] + missing != list(parts)
            or any(
                not _safe_component(item["name"])
                or _receipt_directory_identity(item["identity"]) is None
                for item in existing
            )
        ):
            raise OSError("migration root receipt is malformed")
        mount = receipt["mount_coordinate"]
        if (
            not isinstance(mount, dict)
            or not isinstance(mount["device"], str)
            or not mount["device"]
            or not isinstance(mount["object_path"], str)
            or not Path(mount["object_path"]).is_absolute()
            or type(mount["mount_id"]) is not int
            or mount["mount_id"] <= 0
        ):
            raise OSError("migration root mount receipt is malformed")
    except (KeyError, TypeError, ValueError) as exc:
        raise OSError("migration root receipt is malformed") from exc


def _validate_file_receipt(receipt, *, expected_relative=None) -> None:
    if not isinstance(receipt, dict):
        raise OSError("migration file receipt is missing")
    try:
        if (
            not isinstance(receipt["relative"], list)
            or not isinstance(receipt["parents"], list)
            or not isinstance(receipt["file"], dict)
        ):
            raise OSError("migration file receipt is malformed")
        relative = receipt["relative"]
        parents = receipt["parents"]
        file_receipt = receipt["file"]
        identity = _receipt_identity(file_receipt["identity"])
        digest = file_receipt["sha256"]
        if (
            not relative
            or any(not _safe_component(part) for part in relative)
            or (expected_relative is not None
                and relative != list(expected_relative))
            or any(not isinstance(item, dict) for item in parents)
            or [item["name"] for item in parents] != relative[:-1]
            or any(
                _receipt_directory_identity(item["identity"]) is None
                for item in parents
            )
            or not isinstance(file_receipt, dict)
            or identity is None
            or identity[0] != stat.S_IFREG
            or identity[1] < 0
            or identity[2] <= 0
            or any(
                type(file_receipt[field]) is not int
                for field in (
                    "mode", "uid", "gid", "size",
                    "atime_ns", "mtime_ns", "changed_ns",
                )
            )
            or not stat.S_ISREG(int(file_receipt["mode"]))
            or int(file_receipt["uid"]) < 0
            or int(file_receipt["gid"]) < 0
            or int(file_receipt["size"]) < 0
            or int(file_receipt["atime_ns"]) < 0
            or int(file_receipt["mtime_ns"]) < 0
            or int(file_receipt["changed_ns"]) < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise OSError("migration file receipt is malformed")
        _decode_xattr_snapshot(file_receipt["xattrs"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OSError("migration file receipt is malformed") from exc


def _validate_destination_path_receipt(receipt, dest_rel) -> None:
    if not isinstance(receipt, dict):
        raise OSError("migration destination receipt is missing")
    try:
        if (
            not isinstance(receipt["relative"], list)
            or not isinstance(receipt["parents"], list)
            or not isinstance(receipt["missing_parents"], list)
        ):
            raise OSError("migration destination receipt is malformed")
        relative = receipt["relative"]
        parents = receipt["parents"]
        missing = receipt["missing_parents"]
        if (
            relative != list(Path(dest_rel).parts)
            or any(not _safe_component(part) for part in relative)
            or [item["name"] for item in parents] + missing != relative[:-1]
            or any(
                not isinstance(item, dict)
                or not _safe_component(item["name"])
                or _receipt_directory_identity(item["identity"]) is None
                for item in parents
            )
            or any(not _safe_component(part) for part in missing)
        ):
            raise OSError("migration destination receipt is malformed")
    except (KeyError, TypeError, ValueError) as exc:
        raise OSError("migration destination receipt is malformed") from exc


def _plan_entry_key(source, destination) -> tuple:
    return (
        os.fspath(Path(source)),
        os.fspath(Path(destination)) if destination is not None else None,
    )


def _validate_plan(plan, resume_entries) -> tuple:
    if not isinstance(plan, MigrationPlan):
        raise OSError("migration plan is malformed")
    if (
        not isinstance(plan.entries, list)
        or not isinstance(plan.companion_receipts, list)
        or not isinstance(plan.source_root, Path)
        or not isinstance(plan.dest_root, Path)
    ):
        raise OSError("migration plan is malformed")
    if plan.source_root is None:
        raise OSError("migration source root is missing")
    _validate_root_receipt(plan.source_root_receipt, plan.source_root)
    _validate_root_receipt(plan.dest_root_receipt, plan.dest_root)
    _require_separate_root_receipts(
        plan.source_root_receipt, plan.dest_root_receipt)
    _destination_collision_key(Path("probe"), plan.destination_name_semantics)

    try:
        resume_entries = list(resume_entries or ())
    except (TypeError, ValueError) as exc:
        raise OSError("selected migration resume entries are malformed") from exc
    entry_ids = {id(entry) for entry in plan.entries}
    if (
        len({id(entry) for entry in resume_entries}) != len(resume_entries)
        or any(id(entry) not in entry_ids for entry in resume_entries)
        or any(not _is_resume_collision(entry) for entry in resume_entries)
    ):
        raise OSError("selected migration resume entries are not in the plan")

    placed_keys = set()
    mapped_folders = set()
    audio_relatives = set()
    source_paths = set()
    entry_map = {}
    for entry in plan.entries:
        if not isinstance(entry, PlanEntry) or entry.status not in {
                PLACE, UNPLACEABLE, COLLISION}:
            raise OSError("migration plan entry is malformed")
        if (
            not isinstance(entry.source, Path)
            or not isinstance(entry.source_of_truth, str)
            or not isinstance(entry.reason, str)
            or (entry.meta is not None and not isinstance(entry.meta, dict))
        ):
            raise OSError("migration source path is malformed")
        try:
            _canonical_digest(entry.meta)
            source_relative = _relative_parts(
                entry.source, Path(plan.source_root))
        except (OSError, TypeError, ValueError) as exc:
            raise OSError("migration plan entry is malformed") from exc
        if entry.source_receipt is not None:
            _validate_file_receipt(entry.source_receipt)
            if (
                tuple(entry.source_receipt["relative"])
                != source_relative
            ):
                raise OSError("migration source receipt has the wrong path")
            audio_relatives.add(tuple(entry.source_receipt["relative"]))
        source_key = _path_evidence(entry.source)
        if source_key in source_paths:
            raise OSError("migration plan contains a duplicate source")
        source_paths.add(source_key)
        if entry.dest_rel is not None:
            if not isinstance(entry.dest_rel, Path):
                raise OSError("migration destination path is malformed")
            parts = tuple(Path(entry.dest_rel).parts)
            if (
                Path(entry.dest_rel).is_absolute()
                or not parts
                or any(not _safe_component(part) for part in parts)
            ):
                raise OSError("migration destination path is malformed")
            if entry.source_receipt is not None:
                mapped_folders.add(tuple(entry.source_receipt["relative"][:-1]))
        entry_key = _plan_entry_key(entry.source, entry.dest_rel)
        if entry_key in entry_map:
            raise OSError("migration plan contains a duplicate mapping")
        entry_map[entry_key] = entry
        if entry.status == PLACE:
            if entry.dest_rel is None or entry.destination_receipt is not None:
                raise OSError("migration placement entry is malformed")
            _validate_file_receipt(entry.source_receipt)
            _validate_destination_path_receipt(
                entry.destination_path_receipt, entry.dest_rel)
            key = _destination_collision_key(
                entry.dest_rel, plan.destination_name_semantics)
            if key in placed_keys:
                raise OSError("migration plan contains a destination collision")
            placed_keys.add(key)
        elif entry.status == UNPLACEABLE and (
            entry.dest_rel is not None
            or entry.destination_receipt is not None
            or entry.destination_path_receipt is not None
        ):
            raise OSError("migration unplaceable entry is malformed")
        elif (
            entry.status == COLLISION
            and entry.destination_path_receipt is not None
        ):
            raise OSError("migration collision entry is malformed")
        if entry.destination_receipt is not None:
            if entry.dest_rel is None:
                raise OSError("migration destination receipt has no path")
            _validate_file_receipt(
                entry.destination_receipt,
                expected_relative=Path(entry.dest_rel).parts,
            )
        if (
            entry.destination_path_receipt is not None
            and entry.dest_rel is None
        ):
            raise OSError("migration destination receipt has no path")
        if _is_resume_collision(entry) and (
            entry.source_receipt is None
            or entry.destination_receipt is None
        ):
            raise OSError("migration resume entry is malformed")

    companion_relatives = set()
    for receipt in plan.companion_receipts:
        _validate_file_receipt(receipt)
        relative = tuple(receipt["relative"])
        if (
            relative in companion_relatives
            or relative in audio_relatives
            or relative[:-1] not in mapped_folders
            or Path(relative[-1]).suffix.lower() not in _COMPANION_EXTS
        ):
            raise OSError("migration companion receipt is not in the plan")
        companion_relatives.add(relative)
    return resume_entries, entry_map


def _record_plan_refusal(result, plan, resume_entries, reason) -> ExecResult:
    selected = []
    seen = set()
    entries = getattr(plan, "entries", ())
    if not isinstance(entries, (list, tuple)):
        entries = ()
    placements = [
        entry for entry in entries
        if isinstance(entry, PlanEntry) and entry.status == PLACE
    ]
    for entry in placements + list(resume_entries or ()):
        if (
            not isinstance(entry, PlanEntry)
            or not isinstance(entry.source, Path)
            or (entry.dest_rel is not None
                and not isinstance(entry.dest_rel, Path))
        ):
            continue
        key = (os.fspath(entry.source), os.fspath(entry.dest_rel or ""))
        if key in seen:
            continue
        seen.add(key)
        selected.append(entry)
    for entry in selected:
        result.failed += 1
        result.outcomes.append(
            (entry.source, entry.dest_rel, FAILED, reason))
    return result


def execute_plan(plan: MigrationPlan, *, in_place: bool = False,
                 cancel_check: Optional[Callable[[], bool]] = None,
                 progress: Optional[Callable] = None,
                 resume_entries=None) -> ExecResult:
    from qobuz_librarian.file_exclusion import inode_write_exclusion_scope

    result = None
    try:
        with inode_write_exclusion_scope() as available:
            if not available:
                result = _record_plan_refusal(
                    ExecResult(),
                    plan,
                    resume_entries,
                    "migration could not initialise writer protection",
                )
            else:
                result = _execute_plan(
                    plan,
                    in_place=in_place,
                    cancel_check=cancel_check,
                    progress=progress,
                    resume_entries=resume_entries,
                )
    except BaseException as exc:
        existing_abort = _migration_abort_in_chain(exc)
        if existing_abort is not None:
            if existing_abort is exc:
                raise
            existing_abort.note_cleanup_failure(
                "writer-protection teardown", exc)
            raise existing_abort from existing_abort.cause
        if result is None:
            result = ExecResult()
        result.cancelled = True
        if isinstance(exc, KeyboardInterrupt):
            _record_cleanup_failure(
                result, "writer-protection teardown", exc)
            return result
        abort = MigrationExecutionAbort(result, exc)
        abort.note_cleanup_failure("writer-protection teardown", exc)
        raise abort from exc
    return result


def _execute_plan(plan: MigrationPlan, *, in_place: bool = False,
                  cancel_check: Optional[Callable[[], bool]] = None,
                  progress: Optional[Callable] = None,
                  resume_entries=None) -> ExecResult:
    """Carry out placements and finish verified existing-file resumes.

    Audio is written only for ``PLACE`` entries. Existing-destination
    collisions are read solely to re-prove identity before companion files are
    copied; unplaceable and unresolved collisions stay untouched.

    Copy mode never alters the source. A destination that turns up after
    planning is skipped (not overwritten), and a per-file failure is recorded
    and stepped over so one bad file can't abort the run."""
    result = ExecResult()
    try:
        supplied_resume_entries = list(resume_entries or ())
    except (TypeError, ValueError) as exc:
        return _record_plan_refusal(
            result,
            plan,
            (),
            f"selected migration resume entries are malformed: {exc}",
        )
    try:
        resume_entries, entry_map = _validate_plan(
            plan, supplied_resume_entries)
    except OSError as exc:
        reason = str(exc)
        if "companion" in reason:
            result.companion_outcomes.append((
                Path(plan.source_root or "."), None, FAILED, reason))
        return _record_plan_refusal(
            result, plan, supplied_resume_entries, reason)
    placed = plan.placed
    retired_receipts = []
    source_root = destination_root = None
    try:
        try:
            if (
                plan.source_root is None
                or not isinstance(plan.source_root_receipt, dict)
                or not isinstance(plan.dest_root_receipt, dict)
                or _lexical_absolute(Path(plan.source_root))
                != _lexical_absolute(
                    Path(plan.source_root_receipt.get("path", "")))
                or _lexical_absolute(Path(plan.dest_root))
                != _lexical_absolute(
                    Path(plan.dest_root_receipt.get("path", "")))
            ):
                raise OSError(
                    "migration roots do not match their preview proofs")
            source_root = _open_root_receipt(plan.source_root_receipt)
            destination_root = _open_root_receipt(
                plan.dest_root_receipt, create_missing=True)
            plan.dest_root_receipt = _refresh_root_receipt(destination_root)
        except OSError as exc:
            reason = str(exc)
            return _record_plan_refusal(
                result, plan, resume_entries, reason)

        total = len(placed)
        for i, entry in enumerate(placed, 1):
            if cancel_check and cancel_check():
                result.cancelled = True
                break
            src = entry.source
            if progress:
                progress("Copying into the new library", i, total, src.name)
            opened_source = None
            published = None
            destination_safety = None
            stop_after_item = False
            discard_publication = False
            source_retired = False
            fatal_exception = None
            cleanup_primary = None
            cleanup_failures = []
            recoveries_before_item = len(result.recoveries)
            try:
                if entry.source_receipt is None:
                    raise OSError(
                        "source was not sealed during the migration preview")
                if entry.destination_path_receipt is None:
                    raise OSError(
                        "destination was not sealed during the migration preview")
                if tuple(entry.source_receipt.get("relative") or ()) \
                        != _relative_parts(src, source_root.path):
                    raise OSError("source path does not match its preview proof")
                opened_source = _open_file_receipt(
                    source_root, entry.source_receipt)
                published = _stream_copy_noreplace(
                    opened_source,
                    entry.source_receipt,
                    destination_root,
                    entry.dest_rel,
                    entry.destination_path_receipt,
                )
                if in_place:
                    destination_safety = _hold_published_destination(
                        published,
                        entry.source_receipt["file"],
                    )
                    if _remove_sealed_source(
                            opened_source,
                            entry.source_receipt,
                            destination_safety.intact,
                            result.recoveries):
                        source_retired = True
                        if not destination_safety.release():
                            stop_after_item = destination_safety.interrupted
                            recovery = destination_safety.recovery or {}
                            location = recovery.get("location")
                            location_note = (
                                f" Recovery file: {location}."
                                if location else ""
                            )
                            raise OSError(
                                "migration destination could not be restored "
                                "after source retirement; the exact copy was "
                                "preserved under a private safety name."
                                + location_note
                            )
                        stop_after_item = destination_safety.interrupted
                        retired_receipts.append(entry.source_receipt)
                        result.copied += 1
                        result.outcomes.append(
                            (src, entry.dest_rel, COPIED, ""))
                    else:
                        discard_publication = True
                        raise OSError(
                            "source could not be removed; the same-run "
                            "destination must be rolled back")
                else:
                    result.copied += 1
                    result.outcomes.append(
                        (src, entry.dest_rel, COPIED, ""))
            except _PublishedCopyFailure as exc:
                published = exc.published
                discard_publication = True
                result.failed += 1
                result.outcomes.append(
                    (src, entry.dest_rel, FAILED, str(exc.cause)))
                if exc.interrupted:
                    result.cancelled = True
                    stop_after_item = True
                    cleanup_primary = exc.cause
                elif not isinstance(exc.cause, Exception):
                    result.cancelled = True
                    fatal_exception = exc.cause
            except FileExistsError:
                result.skipped += 1
                result.outcomes.append((
                    src, entry.dest_rel, SKIPPED,
                    "destination appeared after preview",
                ))
            except KeyboardInterrupt as exc:
                discard_publication = (
                    published is not None and not source_retired)
                result.failed += 1
                result.outcomes.append((
                    src,
                    entry.dest_rel,
                    FAILED,
                    "migration was interrupted",
                ))
                result.cancelled = True
                stop_after_item = True
                cleanup_primary = exc
            except (OSError, shutil.Error) as exc:
                discard_publication = (
                    discard_publication
                    or (published is not None and not source_retired)
                )
                result.failed += 1
                result.outcomes.append(
                    (src, entry.dest_rel, FAILED, str(exc)))
                if getattr(exc, "errno", None) == errno.ENOSPC:
                    result.cancelled = True
                    stop_after_item = True
            except BaseException as exc:
                discard_publication = (
                    published is not None and not source_retired)
                result.failed += 1
                result.outcomes.append((
                    src,
                    entry.dest_rel,
                    FAILED,
                    str(exc) or type(exc).__name__,
                ))
                result.cancelled = True
                fatal_exception = exc
            finally:
                if destination_safety is not None:
                    _capture_cleanup(
                        cleanup_failures,
                        "track destination safety teardown",
                        destination_safety.close,
                    )
                    stop_after_item = (
                        stop_after_item or destination_safety.interrupted)
                    if (
                        destination_safety.recovery is not None
                        and destination_safety.recovery
                        not in result.recoveries
                    ):
                        result.recoveries.append(
                            destination_safety.recovery)
                if (
                    discard_publication
                    and not source_retired
                    and published is not None
                ):
                    safe_to_discard = (
                        len(result.recoveries) == recoveries_before_item
                        and (
                            destination_safety is None
                            or (
                                destination_safety.released
                                and destination_safety.recovery is None
                            )
                        )
                    )
                    discarded = False
                    if safe_to_discard:
                        try:
                            discarded = _discard_owned_publication(
                                published,
                                (entry.source_receipt or {}).get("file", {}),
                                recoveries=result.recoveries,
                            )
                        except BaseException as exc:
                            cleanup_failures.append((
                                "track publication rollback", exc))
                    if not discarded:
                        try:
                            recovery = _owned_publication_recovery(
                                published,
                                src,
                                entry.source_receipt or {},
                                "source retirement or publication completion "
                                "failed and the exact same-run destination could "
                                "not be removed safely",
                            )
                            if recovery not in result.recoveries:
                                result.recoveries.append(recovery)
                        except BaseException as exc:
                            cleanup_failures.append((
                                "track recovery recording", exc))
                        if in_place:
                            result.lingered += 1
                _capture_cleanup(
                    cleanup_failures,
                    "track destination teardown",
                    lambda: _close_published_file(published),
                )
                if opened_source is not None:
                    _capture_cleanup(
                        cleanup_failures,
                        "track source teardown",
                        opened_source.close,
                    )
            _raise_cleanup_abort(
                result,
                cleanup_failures,
                primary=(
                    fatal_exception
                    if fatal_exception is not None
                    else cleanup_primary
                ),
            )
            if fatal_exception is not None:
                raise MigrationExecutionAbort(
                    result, fatal_exception) from fatal_exception
            if stop_after_item:
                result.cancelled = True
                break
        if not result.cancelled and plan.companion_receipts:
            _carry_companion_files(
                plan, result, entry_map=entry_map,
                resume_entries=resume_entries,
                progress=progress, cancel_check=cancel_check,
                source_root_binding=source_root,
                destination_root_binding=destination_root)
        if in_place and not result.cancelled:
            _prune_retired_source_parents(
                source_root,
                retired_receipts,
                recoveries=result.recoveries,
                result=result,
                cancel_check=cancel_check,
            )
        return result
    except KeyboardInterrupt:
        result.cancelled = True
        return result
    except MigrationExecutionAbort:
        raise
    except BaseException as exc:
        result.cancelled = True
        raise MigrationExecutionAbort(result, exc) from exc
    finally:
        active_abort = _migration_abort_in_chain(sys.exception())
        cleanup_failures = []
        for boundary, binding in (
            ("destination root teardown", destination_root),
            ("source root teardown", source_root),
        ):
            if binding is None:
                continue
            try:
                binding.close()
            except BaseException as exc:
                cleanup_failures.append((boundary, exc))
        if cleanup_failures:
            if active_abort is not None:
                for boundary, exc in cleanup_failures:
                    active_abort.note_cleanup_failure(boundary, exc)
            else:
                cleanup_abort = MigrationExecutionAbort(
                    result, cleanup_failures[0][1])
                for boundary, exc in cleanup_failures:
                    cleanup_abort.note_cleanup_failure(boundary, exc)
                raise cleanup_abort from cleanup_abort.cause


_CSV_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe_cell(value, *, path=False) -> str:
    """Return a UTF-8 and spreadsheet-safe human display value.

    Machine comparison never trusts these display cells; exact byte paths and
    receipts live in the row/context seals. Only unusual names need the compact
    base64 form, so ordinary audit CSVs remain easy to read.
    """
    if value is None:
        return ""
    if path:
        raw = os.fsencode(value)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return "path-bytes-base64:" + base64.b64encode(raw).decode("ascii")
    else:
        text = str(value)
        try:
            raw = text.encode("utf-8")
        except UnicodeEncodeError:
            raw = text.encode("utf-8", "surrogateescape")
            return "text-bytes-base64:" + base64.b64encode(raw).decode("ascii")
    if (
        text.startswith(_CSV_DANGEROUS_PREFIXES)
        or text.startswith("\ufeff")
        or (text and text[0].isspace())
        or any(ord(character) < 32 for character in text)
    ):
        return "text-base64:" + base64.b64encode(raw).decode("ascii")
    return text


def _audit_filename(prefix) -> str:
    moment = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    return f"{prefix}-{moment}-{secrets.token_hex(16)}.csv"


def _write_csv_for_plan(plan, path, prefix, header, row_builder) -> dict:
    if not isinstance(plan, MigrationPlan):
        raise OSError("migration audit plan is malformed")
    explicit_path = path is not None
    if explicit_path:
        path = _lexical_absolute(Path(path))
        if (
            path.parent != _lexical_absolute(Path(plan.dest_root))
            or not _safe_component(path.name)
        ):
            raise OSError(
                "migration audit file is outside the sealed destination")
    binding = _open_root_receipt(
        plan.dest_root_receipt, create_missing=True)
    try:
        if not binding.matches_public():
            raise OSError(
                "migration destination changed before audit-file write")
        # If the preview safely created a missing destination root, everything
        # after this point binds the exact directory that now exists.
        plan.dest_root_receipt = _refresh_root_receipt(binding)
        _resume_entries, entry_map = _validate_plan(plan, [])
        context = _plan_context(plan)
        context_seal = _canonical_digest(context)
        rows = row_builder(context_seal, entry_map)
        if not isinstance(rows, list):
            raise OSError("migration audit rows are malformed")

        attempts = 1 if explicit_path else 16
        for attempt in range(attempts):
            candidate_name = path.name if explicit_path else _audit_filename(prefix)
            temporary_name = None
            temporary_fd = None
            published = False
            try:
                temporary_name, temporary_fd = _reserve_temporary_file(
                    binding.root_fd)
                with os.fdopen(
                    os.dup(temporary_fd),
                    "w",
                    newline="",
                    encoding="utf-8",
                ) as file_handle:
                    writer = csv.writer(file_handle)
                    writer.writerow([
                        _csv_safe_cell(value) for value in header
                    ])
                    for row in rows:
                        writer.writerow([
                            _csv_safe_cell(value) for value in row
                        ])
                    file_handle.flush()
                    os.fsync(file_handle.fileno())
                try:
                    _rename_noreplace_at(
                        binding.root_fd,
                        temporary_name,
                        binding.root_fd,
                        candidate_name,
                    )
                except BaseException as exc:
                    published = _named_entry_matches(
                        binding.root_fd, candidate_name, temporary_fd)
                    if _named_entry_matches(
                            binding.root_fd, temporary_name, temporary_fd):
                        _remove_exact_named_file(
                            binding.root_fd, temporary_name, temporary_fd)
                    if published:
                        try:
                            _fsync_directories(binding.root_fd)
                        except OSError:
                            pass
                    if (
                        not explicit_path
                        and isinstance(exc, FileExistsError)
                        and not published
                        and attempt + 1 < attempts
                    ):
                        continue
                    raise
                published = True
                if (
                    not _named_entry_matches(
                        binding.root_fd, candidate_name, temporary_fd)
                    or not _named_entry_missing(
                        binding.root_fd, temporary_name)
                    or not binding.matches_public()
                ):
                    raise OSError(
                        "migration audit-file publication was ambiguous")
                _fsync_directories(binding.root_fd)
                receipt = {
                    "relative": [candidate_name],
                    "parents": [],
                    "file": _file_receipt(
                        temporary_fd, _digest_fd(temporary_fd)),
                }
                if not _file_receipt_matches(
                        temporary_fd, receipt["file"]):
                    raise OSError(
                        "migration audit file changed during publication")
                return {
                    "path": os.fspath(binding.path / candidate_name),
                    "receipt": receipt,
                    "context": context,
                }
            except BaseException:
                if (
                    temporary_fd is not None
                    and not published
                    and temporary_name is not None
                    and _named_entry_matches(
                        binding.root_fd, temporary_name, temporary_fd)
                ):
                    _remove_exact_named_file(
                        binding.root_fd, temporary_name, temporary_fd)
                raise
            finally:
                if temporary_fd is not None:
                    try:
                        os.close(temporary_fd)
                    except OSError:
                        pass
        raise OSError("could not reserve a unique migration audit filename")
    finally:
        binding.close()


def _manifest_rows(plan, context_seal) -> list:
    rows = [[
        "context", "", "", "", "", "", "", context_seal,
    ]]
    for entry in plan.entries:
        rows.append([
            "entry",
            entry.status,
            entry.source_of_truth,
            _csv_safe_cell(entry.source, path=True),
            _csv_safe_cell(entry.dest_rel, path=True)
            if entry.dest_rel is not None else "",
            entry.reason,
            _entry_seal(entry),
            context_seal,
        ])
    return rows


def write_manifest(plan: MigrationPlan, path: Optional[Path] = None) -> dict:
    """Durably publish the full reviewed plan and return its exact receipt."""
    return _write_csv_for_plan(
        plan,
        path,
        "migration-manifest",
        [
            "record", "status", "source_of_truth", "source",
            "destination", "reason", "entry_seal", "context_seal",
        ],
        lambda context_seal, _entry_map: _manifest_rows(plan, context_seal),
    )


def _result_entry_seal(entry_map, source, destination) -> str:
    entry = entry_map.get(_plan_entry_key(source, destination))
    return _entry_seal(entry) if entry is not None else ""


def _results_rows(result, plan, context_seal, entry_map) -> list:
    rows = [[
        "context", "", "", "", "", "", "", "", context_seal,
    ]]
    companion_failures = sum(
        status == FAILED
        for _source, _destination, status, _reason
        in result.companion_outcomes
    )
    state = (
        "cancelled" if result.cancelled
        else "failed" if (
            result.failed or companion_failures or result.recoveries
            or result.cleanup_failures)
        else "completed"
    )
    summary = {
        "state": state,
        "cancelled": bool(result.cancelled),
        "copied": int(result.copied),
        "skipped": int(result.skipped),
        "failed": int(result.failed),
        "lingered": int(result.lingered),
        "companions_copied": int(result.companions),
        "companion_attempts": len(result.companion_outcomes),
        "companion_failures": int(companion_failures),
        "pruned": int(result.pruned),
        "track_attempts": len(result.outcomes),
        "recoveries": len(result.recoveries),
        "cleanup_failures": len(result.cleanup_failures),
    }
    rows.append([
        "summary",
        state,
        "",
        "",
        json.dumps(
            summary,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ),
        state,
        "",
        _canonical_digest(summary),
        context_seal,
    ])
    for record, outcomes in (
        ("track", result.outcomes),
        ("companion", result.companion_outcomes),
    ):
        for source, destination, status, reason in outcomes:
            rows.append([
                record,
                status,
                _csv_safe_cell(source, path=True),
                _csv_safe_cell(destination, path=True)
                if destination is not None else "",
                reason,
                "",
                "",
                _result_entry_seal(entry_map, source, destination)
                if record == "track" else "",
                context_seal,
            ])
    for recovery in result.recoveries:
        rows.append([
            "recovery",
            "retained",
            _csv_safe_cell(recovery.get("location", ""), path=True),
            _csv_safe_cell(recovery.get("destination", ""), path=True),
            recovery.get("reason", ""),
            recovery.get("state", ""),
            recovery.get("restart", ""),
            _canonical_digest(recovery),
            context_seal,
        ])
    for failure in result.cleanup_failures:
        rows.append([
            "cleanup",
            FAILED,
            "",
            "",
            failure.get("error", "migration cleanup was uncertain"),
            failure.get("boundary", "migration teardown"),
            "Inspect the partial result and both migration roots before "
            "starting another migration.",
            _canonical_digest(failure),
            context_seal,
        ])
    return rows


def write_results_manifest(
    result: ExecResult,
    path: Optional[Path] = None,
    *,
    plan: MigrationPlan,
) -> dict:
    """Durably publish every track, companion, and recovery outcome."""
    if not isinstance(result, ExecResult):
        raise OSError("migration result is malformed")
    return _write_csv_for_plan(
        plan,
        path,
        "migration-results",
        [
            "record", "status", "source", "destination", "reason",
            "state", "restart", "evidence_seal", "context_seal",
        ],
        lambda context_seal, entry_map: _results_rows(
            result, plan, context_seal, entry_map),
    )


def verify_audit_artifact(plan: MigrationPlan, artifact) -> bool:
    """Verify an exact preview artifact authorises this selected sub-plan."""
    binding = opened = None
    try:
        if not isinstance(artifact, dict):
            return False
        path = _lexical_absolute(Path(artifact["path"]))
        receipt = artifact["receipt"]
        context = artifact["context"]
        if (
            not isinstance(context, dict)
            or path.parent != _lexical_absolute(plan.dest_root)
            or not _safe_component(path.name)
        ):
            return False
        _resume_entries, _entry_map = _validate_plan(
            plan,
            [entry for entry in plan.entries if _is_resume_collision(entry)],
        )
        _validate_file_receipt(receipt, expected_relative=[path.name])
        if receipt["parents"]:
            return False
        if (
            context.get("source_root") != _path_evidence(plan.source_root)
            or context.get("destination_root")
            != _path_evidence(plan.dest_root)
            or context.get("source_root_receipt")
            != plan.source_root_receipt
            or context.get("destination_root_receipt")
            != plan.dest_root_receipt
            or context.get("destination_name_semantics")
            != plan.destination_name_semantics
        ):
            return False
        full_companions = context.get("companion_receipts")
        if not isinstance(full_companions, list):
            return False
        full_companion_seals = set()
        for companion in full_companions:
            _validate_file_receipt(companion)
            seal = _canonical_digest(companion)
            if seal in full_companion_seals:
                return False
            full_companion_seals.add(seal)
        if any(
            _canonical_digest(companion) not in full_companion_seals
            for companion in plan.companion_receipts
        ):
            return False

        binding = _open_root_receipt(plan.dest_root_receipt)
        opened = _open_file_receipt(binding, receipt)
        with os.fdopen(
            os.dup(opened.descriptor),
            "r",
            newline="",
            encoding="utf-8",
        ) as file_handle:
            rows = list(csv.reader(file_handle))
        if not opened.still_exact(receipt):
            return False
        expected_header = [
            "record", "status", "source_of_truth", "source",
            "destination", "reason", "entry_seal", "context_seal",
        ]
        if not rows or rows[0] != expected_header:
            return False
        data = rows[1:]
        if (
            not data
            or len(data[0]) != len(expected_header)
            or data[0][0] != "context"
            or any(data[0][index] for index in range(1, 7))
        ):
            return False
        context_seal = _canonical_digest(context)
        if data[0][7] != context_seal:
            return False
        entry_counts = {}
        for row in data[1:]:
            if (
                len(row) != len(expected_header)
                or row[0] != "entry"
                or row[7] != context_seal
                or len(row[6]) != 64
            ):
                return False
            entry_counts[row[6]] = entry_counts.get(row[6], 0) + 1
        if any(count != 1 for count in entry_counts.values()):
            return False
        selected_seals = [_entry_seal(entry) for entry in plan.entries]
        return (
            len(set(selected_seals)) == len(selected_seals)
            and all(entry_counts.get(seal) == 1 for seal in selected_seals)
        )
    except (
        KeyError,
        OSError,
        TypeError,
        UnicodeError,
        ValueError,
        csv.Error,
    ):
        return False
    finally:
        if opened is not None:
            opened.close()
        if binding is not None:
            binding.close()
