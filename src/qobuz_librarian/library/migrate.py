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
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional

from qobuz_librarian import config, disk_support
from qobuz_librarian.library.scanner import SYSTEM_FOLDER_NAMES
from qobuz_librarian.library.tags import (
    VA_NORMALIZED,
    beets_sanitize,
    known_year,
    normalize,
    strip_year_decoration,
)
from qobuz_librarian.safefile import digest_fd as _digest_fd
from qobuz_librarian.safefile import fsync_dirs, stat_identity
from qobuz_librarian.safefile import named_entry_matches as _named_entry_matches
from qobuz_librarian.safefile import named_entry_missing as _named_entry_missing
from qobuz_librarian.safefile import rename_noreplace as _rename_noreplace_at
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

_FINGERPRINT_FORMATS = {
    ".flac": "flac", ".m4a": "mov", ".mp3": "mp3", ".aac": "aac",
    ".ogg": "ogg", ".opus": "ogg", ".wav": "wav",
}
_FINGERPRINT_CODECS = (
    "aac,aac_fixed,alac,flac,mp3,mp3float,opus,libopus,vorbis,libvorbis,speex,libspeex,"
    "pcm_u8,pcm_s8,pcm_s16le,pcm_s16be,pcm_u16le,pcm_u16be,"
    "pcm_s24le,pcm_s24be,pcm_u24le,pcm_u24be,"
    "pcm_s32le,pcm_s32be,pcm_u32le,pcm_u32be,pcm_s64le,pcm_s64be,"
    "pcm_f32le,pcm_f32be,pcm_f64le,pcm_f64be,pcm_alaw,pcm_mulaw,"
    "adpcm_ima_wav,adpcm_ms"
)

PLACE = "place"
UNPLACEABLE = "unplaceable"
COLLISION = "collision"

COPIED = "copied"
SKIPPED = "skipped"
FAILED = "failed"
INTERRUPTED = "interrupted"


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
    # once every track of their source folder has left.
    pruned: int = 0
    # In-place runs: source folders every track left that still hold files,
    # as (relative path, names of what did not reach the destination). Cover
    # art and sidecars are copied, not moved, so a folder holding only those
    # names nothing.
    left_in_source: list = field(default_factory=list)
    cancelled: bool = False
    interrupted: bool = False
    # One (source, dest_rel, status, reason) per attempted file - the record of
    # what actually happened, surfaced to the user and written to the results
    # manifest. status is COPIED | SKIPPED | FAILED | INTERRUPTED.
    outcomes: list = field(default_factory=list)
    # Companion work has its own count and must not distort the track summary,
    # but every attempt still belongs in the durable results evidence.
    companion_outcomes: list = field(default_factory=list)

    @property
    def failures(self) -> list:
        return [(src, reason) for src, _, status, reason in self.outcomes
                if status == FAILED]


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
    return int(known_year(m.group(1)) or 0) if m else 0


def _is_compilation(tags: Mapping, albumartist: str) -> bool:
    flag = _first(tags, "compilation", "cpil", "tcmp").lower()
    if flag in ("1", "yes", "true"):
        return True
    return normalize(albumartist) in VA_NORMALIZED


_DISC_FOLDER_RE = re.compile(r"(?:disc|cd)\s*0*(\d+)", re.IGNORECASE)


def _folder_disc(name: str) -> int:
    """The disc a "Disc 2" / "CD2" folder name stands for, 0 for any other."""
    m = _DISC_FOLDER_RE.match(name or "")
    return int(m.group(1)) if m else 0


def normalize_tags(tags: Mapping, stem: str, ext: str, folder: str = "") -> dict:
    """Map a raw tag mapping to the fields placement needs.

    Pure: takes an already-read mapping (so it's testable without a real audio
    file). ``stem`` is the source filename minus extension, used as the title
    fallback when no title tag is present; ``folder`` is the name of the folder
    holding the file, which gives the disc number when no tag does.
    """
    albumartist = _first(tags, "albumartist", "album artist", "artist")
    track, _ = _num_and_total(_first(tags, "tracknumber", "track"))
    disc, disc_total_inline = _num_and_total(_first(tags, "discnumber", "disc"))
    if not disc:
        disc = _folder_disc(folder)
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
    return normalize_tags(tags, path.stem, path.suffix, path.parent.name)


def is_placeable(meta: Optional[dict]) -> bool:
    """A file can be placed from metadata once it has an album artist and
    album."""
    if not meta:
        return False
    return bool(meta.get("albumartist") and meta.get("album"))


_NAME_MAX = 200  # bytes - what Beets cuts each name to by default


def _truncate_component(name: str, *, ext: str = "", limit: int = _NAME_MAX) -> str:
    """Cap a single path component at the bytes Beets allows by default, so
    a later import keeps the folder Migrate made. A tag long enough to exceed
    the filesystem's own limit would otherwise raise ENAMETOOLONG from the
    first Path.exists()/copy and abort the whole migration before any plan is
    built. Truncates on a UTF-8 char boundary and reserves room for the
    extension."""
    ext_b = ext.encode("utf-8", "surrogateescape")
    budget = max(1, limit - len(ext_b))
    b = name.encode("utf-8", "surrogateescape")
    if len(b) <= budget:
        return name + ext
    # Drop a partial trailing multi-byte char rather than splitting it, and
    # clean the cut name again as Beets does, so a space or dot it ends on
    # goes the same way.
    return beets_sanitize(b[:budget].decode("utf-8", "ignore")) + ext


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


def _album_source_dir(source) -> Path:
    """The folder an album came from, above any "Disc N" folder of its own."""
    parent = Path(source).parent
    return parent.parent if _folder_disc(parent.name) else parent


def companion_folders(folder) -> tuple:
    """The source folders whose cover art and sidecars go with a track in
    ``folder``: its own, and the album folder above a "Disc N" folder."""
    folder = tuple(folder)
    if folder and _folder_disc(folder[-1]):
        return folder, folder[:-1]
    return (folder,)


def _companion_destinations(dest_rel) -> tuple:
    """Where the folders of ``companion_folders`` carry to, for a track
    placed at ``dest_rel``: its own folder, then its album folder."""
    dest_rel = Path(dest_rel)
    return dest_rel.parent, Path(*dest_rel.parts[:2])


def _album_year(metas) -> int:
    """The one year an album's folder is named with: the year most of its
    tracks carry, the later one on a tie."""
    counts = Counter(meta.get("year") or 0 for meta in metas)
    counts.pop(0, None)
    if not counts:
        return 0
    return max(counts, key=lambda year: (counts[year], year))


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
            errno.ENOSYS, "this filesystem doesn't support the identity "
            "check migration needs"
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
        raise OSError("this filesystem doesn't support the identity check "
                       "migration needs")
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
    return os.open(
        os.fspath(path),
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=dir_fd,
    )


def _open_regular_file(name, *, dir_fd) -> int:
    flags = (
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        | os.O_NONBLOCK
    )
    try:
        descriptor = os.open(
            os.fspath(name), flags | os.O_NOATIME, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EPERM):
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


def _file_receipt(descriptor, digest=None) -> dict:
    value = os.fstat(descriptor)
    if not stat.S_ISREG(value.st_mode):
        raise OSError("migration source is not a regular file")
    receipt = {
        "identity": _entry_identity(value),
        "mode": int(value.st_mode),
        "size": int(value.st_size),
        "mtime_ns": int(value.st_mtime_ns),
        "changed_ns": int(value.st_ctime_ns),
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
            or int(value.st_size) != int(receipt["size"])
            or int(value.st_mtime_ns) != int(receipt["mtime_ns"])
            or (not allow_changed_time
                and int(value.st_ctime_ns) != int(receipt["changed_ns"]))
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
                fsync_dirs(parent_fd)
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
    fsync_dirs(parent_fd)
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
                 descriptor, verified_digest):
        self.root = root
        self.parents = parents
        self.parent_names = parent_names
        self.parent_fd = parent_fd
        self.name = name
        self.descriptor = descriptor
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
            and _file_receipt_matches(
                self.descriptor,
                receipt.get("file"),
                digest=self.verified_digest,
            )
        )

    def close(self) -> None:
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
        verified_digest = _digest_fd(file_fd)
        if (
            not binding.matches_public()
            or not _file_receipt_matches(
                file_fd, file_receipt, digest=verified_digest)
        ):
            raise OSError("migration source changed after preview")
        opened = _OpenedFile(
            binding, descriptors, list(parts[:-1]), parent_fd, parts[-1],
            file_fd, verified_digest)
        descriptors = []
        file_fd = None
        return opened
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
    anchor = existing_ancestor(dest_root)
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
            | os.O_NOFOLLOW | os.O_CLOEXEC,
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
                    fsync_dirs(binding.root_fd)
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
        folder
        for entry in entries
        if entry.dest_rel is not None and entry.source_receipt is not None
        for folder in companion_folders(
            entry.source_receipt.get("relative", ())[:-1])
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
                    reason="source could not be read safely", meta=meta))
                continue
            placeable.append((source, meta, sot, source_receipt))
    finally:
        if source_binding is not None:
            source_binding.close()

    # The year is decided once per album, so tracks tagged with different
    # years (a compilation's own, or one original date) share one folder. An
    # album is its tags plus the folder it came from, which keeps two
    # same-titled albums in separate folders apart.
    albums: dict = {}
    for row in placeable:
        source, meta = row[0], row[1]
        albums.setdefault(
            (_album_key({**meta, "year": 0}), _album_source_dir(source)), []
        ).append(row)

    # Group by album so multi-disc can be decided from the whole album, not one
    # track: a disc tag of 2 only means "Disc 2/" if the album really spans
    # discs (disctotal > 1, or some track carries a higher disc number).
    groups: dict = {}
    for members in albums.values():
        year = _album_year(meta for _, meta, _, _ in members)
        for row in members:
            groups.setdefault(
                _album_key({**row[1], "year": year}), []).append(row)

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
                reason="destination folder could not be read safely", meta=meta,
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
        folder
        for entry in entries
        if entry.dest_rel is not None and isinstance(entry.source_receipt, dict)
        for folder in companion_folders(
            entry.source_receipt.get("relative", ())[:-1])
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
    """Resolve an album through AcoustID, or None without a confident match."""
    try:
        import acoustid
        import mutagen
        from mutagen._util import FileThing
    except ImportError:
        vlog("acoustid not installed; skipping fingerprint stage")
        return None
    try:
        suffix = (ext or path.suffix).lower()
        input_format = _FINGERPRINT_FORMATS.get(suffix)
        if input_format is None:
            return None
        source = (f"/proc/self/fd/{int(descriptor)}" if descriptor is not None
                  else str(path))
        with open(source, "rb") as fileobj:
            audio = mutagen.File(FileThing(fileobj, str(path), str(path)))
        if audio is None:
            return None
        input_options = [
            "-f", input_format, "-format_whitelist", input_format,
            "-codec_whitelist", _FINGERPRINT_CODECS,
            "-protocol_whitelist", "file",
        ]
        pass_fds = () if descriptor is None else (int(descriptor),)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", *input_options,
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", source],
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=60, pass_fds=pass_fds,
            env=config.child_env(),
        )
        # ffprobe also opens cover/video decoders, which the whitelist rejects.
        duration = (float(probe.stdout.strip()) if probe.returncode == 0
                    else audio.info.length)
        if not math.isfinite(duration) or duration <= 0:
            return None
        completed = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
             *input_options, "-i", source,
             "-map", "0:a:0", "-vn", "-sn", "-dn", "-t", "120",
             # Match fpcalc's resampler.
             "-af", "aresample=11025:filter_size=16:phase_shift=8:linear_interp=1:cutoff=0.8",
             "-ac", "1", "-ar", "11025", "-f", "chromaprint", "-"],
            check=True,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
            pass_fds=pass_fds,
            env=config.child_env(),
        )
        fp = completed.stdout.strip()
        if not fp:
            return None
        resp = acoustid.lookup(ACOUSTID_API_KEY, fp, duration,
                               meta="recordings releasegroups releases")
    except Exception as e:
        vlog(f"fingerprint failed for {path.name}: {e}")
        return None
    return identify_from_lookup(resp, min_score, path.stem, (ext or path.suffix).lower())


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _sample_file(root):
    """The stat of one file a few folders into ``root``, found without
    walking the tree, or None."""
    folder = root
    for _ in range(4):
        try:
            with os.scandir(folder) as listing:
                entries = sorted(listing, key=lambda entry: entry.name)
        except OSError:
            return None
        for entry in entries:
            if entry.is_file(follow_symlinks=False):
                try:
                    return entry.stat(follow_symlinks=False)
                except OSError:
                    return None
        folders = [entry for entry in entries
                   if entry.is_dir(follow_symlinks=False)]
        if not folders:
            return None
        folder = folders[0].path
    return None


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
    dest_anchor = dest if dest.exists() else existing_ancestor(dest)
    if dest_anchor is not None and (
            refusal := disk_support.destination_refusal(
                dest_anchor, _sample_file(src))):
        return refusal
    try:
        source_receipt = _capture_root_receipt(src)
        destination_receipt = _capture_root_receipt(dest, allow_missing=True)
        _require_separate_root_receipts(source_receipt, destination_receipt)
    except OSError as exc:
        return f"Source/destination safety could not be proved: {exc}"
    # The copy/move writes into the destination tree (created if absent), so a
    # read-only dest must fail here, not partway through with a raw OSError.
    if dest_anchor is None or not os.access(str(dest_anchor), os.W_OK):
        return f"Destination isn't writable: {dest_anchor or dest}"
    return None


def existing_ancestor(path: Path) -> Optional[Path]:
    """The nearest path that exists at or above ``path`` (where a copy lands)."""
    p = Path(path)
    while True:
        if p.exists():
            return p
        if p.parent == p:
            return None
        p = p.parent


# Hidden names earlier versions used while copying and moving. A migration
# cut off part way could leave them behind.
_LEFTOVER_NAME = re.compile(
    r"\.qobuz-migrate-(copy|dir|name-probe|source|destination|publication"
    r"|discard|empty)-[0-9a-f]{32}"
)


def interrupted_migration_leftovers(*roots) -> list:
    """Hidden items an interrupted migration left under ``roots``.

    They are only named for the user, never moved or deleted: some may hold
    the bytes of a track that was being moved."""
    found = []
    for root in roots:
        for folder, folders, files in os.walk(root):
            matched = sorted(
                name for name in folders + files
                if _LEFTOVER_NAME.fullmatch(name))
            folders[:] = sorted(
                name for name in folders if name not in matched)
            found.extend(Path(folder, name) for name in matched)
    return found


def space_estimate(plan: MigrationPlan, *, in_place: bool = False,
                   resume_entries=None) -> tuple:
    """``(bytes_to_write, free_bytes_at_dest)`` for carrying out ``plan``.

    Only files that actually get written count toward ``bytes_to_write``:
    every placed audio file (in-place mode also creates and flushes a verified
    destination before retiring its source), plus companions for both new
    placements and verified resumes.
    ``free_bytes`` is the space available where the new library is built, or
    None when it can't be read (no existing ancestor, or a stat error)."""
    anchor = existing_ancestor(plan.dest_root)
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
    try:
        descriptor = _open_regular_file(name, dir_fd=parent_fd)
        if not binding.entry_is_on_root_mount(descriptor):
            raise OSError("migration source crosses a nested filesystem mount")
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
            or not binding.matches_public()
        ):
            raise OSError("migration source changed while it was reviewed")
        return display_path, metadata, after
    finally:
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
            if name in SYSTEM_FOLDER_NAMES:
                continue
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
                        else "Reading cover art and sidecars",
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


def _lyric_destinations(entries):
    targets = {}
    for entry in entries:
        if entry.dest_rel is None or entry.source_receipt is None:
            continue
        source = Path(*entry.source_receipt["relative"]).with_suffix("")
        targets.setdefault(source, set()).add(Path(entry.dest_rel).with_suffix(".lrc"))
    return targets


def _earlier_lyric_destinations(plan, source_root_binding,
                                destination_root_binding):
    """Lyric targets for tracks an earlier, interrupted run already moved.

    Such a track is no longer in the source, so this run has no entry for
    it; the plan that run recorded beside the destination says where it
    went. A target is kept only while that track is still there."""
    planned = {
        Path(*entry.source_receipt["relative"]).with_suffix("")
        for entry in plan.entries if entry.source_receipt is not None
    }
    orphans = {
        Path(*receipt["relative"]).with_suffix("")
        for receipt in plan.companion_receipts
        if Path(receipt["relative"][-1]).suffix.lower() == ".lrc"
    } - planned
    if not orphans:
        return {}
    earlier = {}
    root_fd = destination_root_binding.root_fd
    for name in sorted(os.listdir(root_fd)):
        if not (name.startswith("migration-manifest-")
                and name.endswith(".csv")):
            continue
        try:
            with os.fdopen(_open_regular_file(name, dir_fd=root_fd),
                           newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    if row.get("status") == PLACE and row.get("destination"):
                        earlier[row.get("source")] = row["destination"]
        except (OSError, UnicodeError, csv.Error):
            continue
    targets = {}
    for source, destination in earlier.items():
        try:
            relative = Path(source).relative_to(source_root_binding.path)
        except (TypeError, ValueError):
            continue
        stem = relative.with_suffix("")
        if stem not in orphans or relative.suffix.lower() not in config.AUDIO_EXTS:
            continue
        try:
            parents, parent_fd, audio = _open_destination_parent(
                destination_root_binding, Path(destination))
        except OSError:
            continue
        try:
            value = os.stat(audio, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISREG(value.st_mode):
                targets[stem] = {Path(destination).with_suffix(".lrc")}
        except OSError:
            pass
        finally:
            for descriptor in reversed(parents):
                os.close(descriptor)
    return targets


def _ambiguous_lyric_targets(plan, entries):
    targets = _lyric_destinations(entries)
    sources, destinations = {}, {}
    for receipt in plan.companion_receipts:
        relative = Path(*receipt["relative"])
        if relative.suffix.lower() != ".lrc":
            continue
        for target in targets.get(relative.with_suffix(""), ()):
            key = _destination_collision_key(target, plan.destination_name_semantics)
            sources.setdefault(key, set()).add(relative)
            destinations.setdefault(key, set()).add(target)
    return {target for key, paths in destinations.items() if len(sources[key]) > 1
            for target in paths}


def _companion_targets(companions, folder, lyric_destinations):
    for receipt in companions:
        relative = Path(*receipt["relative"])
        if relative.suffix.lower() == ".lrc":
            for target in sorted(lyric_destinations.get(relative.with_suffix(""), ())):
                if target.parent == folder:
                    yield receipt, target
        else:
            yield receipt, folder / relative.name


def _carry_companion_files(plan: "MigrationPlan", result: "ExecResult", *,
                           entry_map,
                           resume_entries=None,
                           progress: Optional[Callable] = None,
                           cancel_check: Optional[Callable[[], bool]] = None,
                           source_root_binding=None,
                           destination_root_binding=None,
                           copied_companions,
                           lyric_conflicts,
                           retired=None,
                           ) -> None:
    """Copy each migrated album folder's non-audio companions into the
    destination folder(s) that received its audio. Always a copy (never a
    move), even in in-place mode: the same cover can fan out to several
    destinations (for example, AcoustID splitting one source folder), the
    source folder may still hold audio that failed/skipped, and a duplicated
    cover image is harmless.

    ``copied_companions`` records copies completed as in-place tracks landed.

    With ``retired`` (a list, in-place runs), the original of each verified
    existing copy is deleted, finishing a move an earlier run was cut off
    from, and its receipt is added to the list.
    """
    folder_map: dict = {}
    landed = []

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
        for source_folder, destination in zip(
                companion_folders(entry.source_receipt["relative"][:-1]),
                _companion_destinations(entry.dest_rel)):
            folder_map.setdefault(source_folder, set()).add(destination)
        landed.append(entry)
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
        remembered = finished = False
        kept = ""
        opened_source = opened_destination = None
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
            if remembered and retired is not None:
                source_stat = os.fstat(opened_source.descriptor)
                destination_stat = os.fstat(opened_destination.descriptor)
                published = (
                    opened_destination.descriptor,
                    opened_destination.parents,
                    opened_destination.parent_fd,
                    opened_destination.name,
                    Path(entry.dest_rel),
                    destination_root_binding,
                )
                # Two network or FUSE mounts of one share show the same file
                # under different device numbers. Deleting the "original"
                # there would delete the only copy.
                if (
                    source_stat.st_ino == destination_stat.st_ino
                    and source_stat.st_size == destination_stat.st_size
                ):
                    kept = ("the existing copy may be the original itself, "
                            "seen through another folder, so the original "
                            "was kept")
                else:
                    finished = _retire_source(
                        opened_source, entry.source_receipt, published,
                        stat_identity(destination_stat))
                    if not finished:
                        kept = ("the original or the existing copy changed, "
                                "so the original was kept")
                if finished:
                    retired.append(entry.source_receipt)
                else:
                    result.lingered += 1
        except KeyboardInterrupt:
            result.cancelled = True
            result.interrupted = True
            result.outcomes.append((
                entry.source,
                entry.dest_rel,
                INTERRUPTED,
                "migration was interrupted",
            ))
            outcome_recorded = True
        except OSError:
            remembered = False
        except Exception as exc:
            log.exception("Migration stopped at %s", entry.source)
            result.cancelled = True
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
                opened_destination.close()
            if opened_source is not None:
                opened_source.close()
        if not outcome_recorded:
            if finished:
                result.copied += 1
                result.outcomes.append((
                    entry.source, entry.dest_rel, COPIED,
                    "finished an earlier move"))
            elif remembered:
                result.skipped += 1
                result.outcomes.append((
                    entry.source, entry.dest_rel, SKIPPED,
                    f"verified existing copy; {kept}" if kept
                    else "verified existing copy"))
            else:
                result.failed += 1
                result.outcomes.append((
                    entry.source, entry.dest_rel, FAILED,
                    "existing destination could not be reverified"))
        if _cancelled():
            return

    receipts_by_folder: dict = {}
    for receipt in plan.companion_receipts:
        try:
            folder = tuple(receipt["relative"][:-1])
        except (KeyError, TypeError):
            continue
        receipts_by_folder.setdefault(folder, []).append(receipt)

    lyric_destinations = _lyric_destinations(landed)
    for stem, targets in _earlier_lyric_destinations(
            plan, source_root_binding, destination_root_binding).items():
        lyric_destinations.setdefault(stem, targets)
    for src_folder, dst_folders in folder_map.items():
        if _cancelled():
            return
        if not _copy_companions(
                result, receipts_by_folder.get(src_folder, ()), dst_folders,
                progress=progress, cancelled=_cancelled,
                source_root_binding=source_root_binding,
                destination_root_binding=destination_root_binding,
                lyric_destinations=lyric_destinations,
                copied_companions=copied_companions,
                lyric_conflicts=lyric_conflicts):
            return


_SAME_COMPANION = "destination already holds the same file"


def _destination_holds(binding, destination_path, receipt) -> bool:
    """Whether the file already at ``destination_path`` has the bytes the
    source receipt sealed."""
    try:
        parents, parent_fd, name = _open_destination_parent(
            binding, destination_path)
    except OSError:
        return False
    descriptor = None
    try:
        descriptor = _open_regular_file(name, dir_fd=parent_fd)
        return (
            os.fstat(descriptor).st_size == receipt["file"]["size"]
            and _digest_fd(descriptor) == receipt["file"]["sha256"]
        )
    except OSError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for parent in reversed(parents):
            os.close(parent)


def _copy_companions(result, companions, dst_folders, *, progress, cancelled,
                     source_root_binding, destination_root_binding,
                     lyric_destinations, copied_companions, lyric_conflicts) -> bool:
    """Copy one source folder's companions into each of ``dst_folders``.
    Returns False once the run has been cancelled."""
    for dst_folder in dst_folders:
        for receipt, destination_path in _companion_targets(
                companions, Path(dst_folder), lyric_destinations):
            key = (tuple(receipt["relative"]), destination_path)
            if key in copied_companions:
                continue
            if cancelled():
                return False
            copied_companions.add(key)
            opened = None
            published = None
            discard_publication = False
            name = Path(receipt["relative"][-1]).name
            source_path = source_root_binding.path.joinpath(
                *receipt["relative"])
            try:
                if destination_path in lyric_conflicts:
                    raise OSError("multiple lyric files map to this destination")
                if progress:
                    progress(
                        "Carrying cover art and sidecars", 0, 0, name)
                if cancelled():
                    return False
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
            except FileExistsError:
                result.companion_outcomes.append((
                    source_path,
                    destination_path,
                    SKIPPED,
                    _SAME_COMPANION if _destination_holds(
                        destination_root_binding, destination_path, receipt)
                    else "destination already exists",
                ))
            except KeyboardInterrupt:
                discard_publication = published is not None
                result.cancelled = True
                result.interrupted = True
                result.companion_outcomes.append((
                    source_path,
                    destination_path,
                    INTERRUPTED,
                    "migration was interrupted",
                ))
            except (OSError, shutil.Error) as exc:
                log.info(f"  ⚠  couldn't carry {name}: {exc}")
                if getattr(exc, "errno", None) == errno.ENOSPC:
                    result.cancelled = True
                result.companion_outcomes.append((
                    source_path,
                    destination_path,
                    FAILED,
                    str(exc),
                ))
            except Exception as exc:
                log.exception("Migration stopped at %s", source_path)
                discard_publication = published is not None
                result.cancelled = True
                result.companion_outcomes.append((
                    source_path,
                    destination_path,
                    FAILED,
                    str(exc) or type(exc).__name__,
                ))
            finally:
                if (
                    discard_publication
                    and published is not None
                    and not _take_back(published, opened, receipt)
                ):
                    _note_kept_copy(
                        result.companion_outcomes, destination_root_binding)
                _close_published_file(published)
                if opened is not None:
                    opened.close()

    return True


def collect_items(source_root: Path, *, use_acoustid: bool = False,
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
    for _ in range(16):
        name = f".qobuz-migrate-copy-{secrets.token_hex(16)}"
        try:
            descriptor = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                | os.O_CLOEXEC,
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


def _retire_source(source: _OpenedFile, receipt, published,
                   published_identity) -> bool:
    """Delete the reviewed original once its checked copy is on disk.

    The copy and its folder are already flushed. Both are checked once more
    first: the copy is still the file this run published, unchanged, and the
    original still matches its preview receipt. The original is removed
    through its held folder, so a renamed or swapped folder above it is
    never followed."""
    descriptor = published[0]
    try:
        if (
            not _published_file_still_exact(published)
            or stat_identity(os.fstat(descriptor)) != published_identity
            or not _file_fidelity_matches(descriptor, receipt["file"])
            or not source.still_exact(receipt)
        ):
            return False
        os.unlink(source.name, dir_fd=source.parent_fd)
    except OSError:
        return False
    try:
        fsync_dirs(source.parent_fd)
    except OSError:
        pass
    return True


def _remove_exact_named_file(parent_fd, name, descriptor) -> bool:
    if not _named_entry_matches(parent_fd, name, descriptor):
        return False
    try:
        os.unlink(name, dir_fd=parent_fd)
    except OSError:
        return False
    return _named_entry_missing(parent_fd, name)


def _file_fidelity_matches(descriptor, receipt, *, digest=None) -> bool:
    """Verify copied metadata, and optionally an already-computed digest."""
    try:
        value = os.fstat(descriptor)
        if (
            not stat.S_ISREG(value.st_mode)
            or int(value.st_mode) != int(receipt["mode"])
            or int(value.st_size) != int(receipt["size"])
            or int(value.st_mtime_ns) != int(receipt["mtime_ns"])
        ):
            return False
        return digest is None or digest == receipt["sha256"]
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _apply_file_metadata(destination_fd, source_receipt) -> None:
    """Copy and verify the mode and modification time."""
    expected = source_receipt["file"]
    os.fchmod(destination_fd, stat.S_IMODE(int(expected["mode"])))
    os.utime(
        destination_fd,
        ns=(os.fstat(destination_fd).st_atime_ns, int(expected["mtime_ns"])),
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
                    fsync_dirs(parent_fd)
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
        fsync_dirs(parent_fd)
        return (
            temporary_fd, parents, parent_fd, destination_name,
            Path(dest_rel), destination,
        )
    except BaseException:
        if published and temporary_fd is not None:
            publication = (
                temporary_fd, parents, parent_fd, destination_name,
                Path(dest_rel), destination,
            )
            if not _take_back(publication, source, source_receipt):
                log.warning(
                    "Migration kept the new copy at %s",
                    destination.path / Path(dest_rel),
                )
        elif (
            temporary_fd is not None
            and temporary_name is not None
            and _named_entry_matches(parent_fd, temporary_name, temporary_fd)
        ):
            _remove_exact_named_file(parent_fd, temporary_name, temporary_fd)
        if temporary_fd is not None:
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


def _take_back(published, source: _OpenedFile, receipt) -> bool:
    """Remove a copy this run published while its original is still there.

    Nothing is removed when the original's name no longer holds the original
    file, or when the copy was moved, replaced or changed since it was
    published."""
    descriptor, _parents, parent_fd, name, _dest_rel, _binding = published
    try:
        if (
            not _named_entry_matches(
                source.parent_fd, source.name, source.descriptor)
            or not _published_file_still_exact(published)
            or not _file_fidelity_matches(
                descriptor, receipt["file"], digest=_digest_fd(descriptor))
            or not _remove_exact_named_file(parent_fd, name, descriptor)
        ):
            return False
    except OSError:
        return False
    try:
        fsync_dirs(parent_fd)
    except OSError:
        pass
    return True


def _note_kept_copy(outcomes, destination_root) -> None:
    """Name the kept copy in the reason of the latest outcome."""
    source, destination, status, reason = outcomes[-1]
    outcomes[-1] = (
        source, destination, status,
        f"{reason}; the new copy was kept at "
        f"{destination_root.path / destination}",
    )


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
            raise OSError("private migration directory changed")
        return name, descriptor
    raise OSError("could not reserve a private migration directory")


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


def _remove_empty_sealed_directory(binding, chain) -> bool:
    descriptors = _open_sealed_directory_chain(binding, chain)
    parent_fd = binding.root_fd if len(descriptors) == 1 else descriptors[-2]
    try:
        os.rmdir(chain[-1]["name"], dir_fd=parent_fd)
        return True
    except OSError:
        return False
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _prune_retired_source_parents(binding, receipts, *, result=None,
                                  cancel_check=None) -> int:
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
            if _remove_empty_sealed_directory(binding, chain):
                removed += 1
                if result is not None:
                    result.pruned += 1
        except OSError:
            continue
    return removed


def _source_folders_left(binding, chains, carried) -> list:
    """The finished source folders, of ``{folder: sealed parent chain}``, that
    still hold anything, as (relative path, names left out) pairs.

    ``carried`` holds the source paths of the cover art and sidecars that
    reached the destination; every other name is left out, with a slash
    after a folder's, except a finished folder, which is reported itself."""
    left = []
    for folder, chain in sorted(chains.items()):
        if not chain:
            continue
        try:
            descriptors = _open_sealed_directory_chain(binding, list(chain))
        except OSError:
            continue
        try:
            names = sorted(os.listdir(descriptors[-1]))
            if names:
                left_out = []
                for name in names:
                    if (
                        binding.path.joinpath(*folder, name) in carried
                        or (*folder, name) in chains
                    ):
                        continue
                    value = os.stat(
                        name, dir_fd=descriptors[-1], follow_symlinks=False)
                    left_out.append(
                        name + "/" if stat.S_ISDIR(value.st_mode) else name)
                left.append((os.path.join(*folder), left_out))
        except OSError:
            continue
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
    return left


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
                for field in ("mode", "size", "mtime_ns", "changed_ns")
            )
            or not stat.S_ISREG(int(file_receipt["mode"]))
            or int(file_receipt["size"]) < 0
            or int(file_receipt["mtime_ns"]) < 0
            or int(file_receipt["changed_ns"]) < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise OSError("migration file receipt is malformed")
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
                mapped_folders.update(companion_folders(
                    entry.source_receipt["relative"][:-1]))
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
    """Carry out placements and finish verified existing-file resumes.

    Audio is written only for ``PLACE`` entries. Existing-destination
    collisions are read to re-prove identity before companion files are
    copied, and an in-place run then deletes their originals; unplaceable and
    unresolved collisions stay untouched.

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
    # Tracks still to leave each source folder, and the sealed chain of every
    # folder a track has left, for the in-place bookkeeping below.
    waiting = Counter()
    folder_chains: dict = {}

    def finished_folders_left():
        return _source_folders_left(
            source_root,
            {folder: chain for folder, chain in folder_chains.items()
             if not waiting[folder]},
            {source for source, _destination, status, reason
             in result.companion_outcomes
             if status == COPIED or reason == _SAME_COMPANION})

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

        # An in-place run carries a source folder's cover art and sidecars to
        # a destination folder as soon as a track lands there, and removes the
        # folders a source folder emptied once its last track has left. A run
        # stopped part way then leaves nothing behind that a later run, which
        # no longer sees the moved tracks, would miss.
        def source_folder(entry):
            return tuple((entry.source_receipt or {}).get("relative", ())[:-1])

        companions_by_folder: dict = {}
        for receipt in plan.companion_receipts:
            companions_by_folder.setdefault(
                tuple(receipt["relative"][:-1]), []).append(receipt)
        waiting.update(
            each for entry in placed
            for each in companion_folders(source_folder(entry)))
        retired_by_folder: dict = {}
        copied_companions = set()
        lyric_conflicts = _ambiguous_lyric_targets(plan, [*placed, *resume_entries])

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
            stop_after_item = False
            discard_publication = False
            source_retired = False
            moved = False
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
                    published_identity = stat_identity(
                        os.fstat(published[0]))
                    if not _retire_source(
                            opened_source,
                            entry.source_receipt,
                            published,
                            published_identity):
                        discard_publication = True
                        raise OSError(
                            "the original or its new copy changed during the "
                            "move, so the original was not removed")
                    source_retired = True
                    retired_receipts.append(entry.source_receipt)
                    result.copied += 1
                    result.outcomes.append(
                        (src, entry.dest_rel, COPIED, ""))
                    moved = True
                else:
                    result.copied += 1
                    result.outcomes.append(
                        (src, entry.dest_rel, COPIED, ""))
            except FileExistsError:
                result.skipped += 1
                result.outcomes.append((
                    src, entry.dest_rel, SKIPPED,
                    "destination appeared after preview",
                ))
            except KeyboardInterrupt:
                discard_publication = (
                    published is not None and not source_retired)
                result.interrupted = True
                result.outcomes.append((
                    src,
                    entry.dest_rel,
                    INTERRUPTED,
                    "migration was interrupted",
                ))
                result.cancelled = True
                stop_after_item = True
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
            except Exception as exc:
                log.exception("Migration stopped at %s", src)
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
                stop_after_item = True
            finally:
                if (
                    discard_publication
                    and published is not None
                    and not source_retired
                    and not _take_back(
                        published, opened_source, entry.source_receipt)
                ):
                    _note_kept_copy(result.outcomes, destination_root)
                    if in_place and _named_entry_matches(
                            opened_source.parent_fd, opened_source.name,
                            opened_source.descriptor):
                        result.lingered += 1
                _close_published_file(published)
                if opened_source is not None:
                    opened_source.close()
            if stop_after_item:
                result.cancelled = True
                break
            if in_place:
                folder = source_folder(entry)
                folders = companion_folders(folder)
                waiting.subtract(folders)
                if moved:
                    retired_by_folder.setdefault(folder, []).append(
                        entry.source_receipt)
                    for each, destination in zip(
                            folders, _companion_destinations(entry.dest_rel)):
                        folder_chains[each] = \
                            entry.source_receipt["parents"][:len(each)]
                        _copy_companions(
                            result, companions_by_folder.get(each, ()),
                            [destination], progress=None,
                            cancelled=lambda: result.cancelled,
                            source_root_binding=source_root,
                            destination_root_binding=destination_root,
                            lyric_destinations=_lyric_destinations([entry]),
                            copied_companions=copied_companions,
                            lyric_conflicts=lyric_conflicts)
                if not waiting[folder] and not result.cancelled:
                    _prune_retired_source_parents(
                        source_root,
                        retired_by_folder.pop(folder, ()),
                        result=result,
                        cancel_check=cancel_check,
                    )
                if result.cancelled:
                    break
        if not result.cancelled and (
                plan.companion_receipts or (in_place and resume_entries)):
            finished_moves = [] if in_place else None
            try:
                _carry_companion_files(
                    plan, result, entry_map=entry_map,
                    resume_entries=resume_entries,
                    progress=progress, cancel_check=cancel_check,
                    source_root_binding=source_root,
                    destination_root_binding=destination_root,
                    copied_companions=copied_companions,
                    lyric_conflicts=lyric_conflicts,
                    retired=finished_moves)
            finally:
                for receipt in finished_moves or ():
                    retired_receipts.append(receipt)
                    for each in companion_folders(receipt["relative"][:-1]):
                        folder_chains[each] = receipt["parents"][:len(each)]
        if in_place and not result.cancelled:
            _prune_retired_source_parents(
                source_root,
                retired_receipts,
                result=result,
                cancel_check=cancel_check,
            )
        if in_place:
            result.left_in_source = finished_folders_left()
        return result
    except (KeyboardInterrupt, Exception) as exc:
        # The run stops here, but returns its result so the report is
        # written: originals may already have been deleted.
        if isinstance(exc, KeyboardInterrupt):
            result.interrupted = True
        else:
            log.exception("Migration stopped by an unexpected error")
        result.cancelled = True
        if in_place and folder_chains:
            try:
                result.left_in_source = finished_folders_left()
            except BaseException:
                pass
        return result
    finally:
        if destination_root is not None:
            destination_root.close()
        if source_root is not None:
            source_root.close()


_CSV_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe_cell(value, *, path=False) -> str:
    """Return a UTF-8 and spreadsheet-safe human display value.

    Only unusual names need the compact base64 form, so ordinary audit CSVs
    remain easy to read.
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


def _write_csv_for_plan(plan, prefix, header, rows) -> Path:
    binding = _open_root_receipt(
        plan.dest_root_receipt, create_missing=True)
    try:
        if not binding.matches_public():
            raise OSError(
                "migration destination changed before audit-file write")
        # If the preview safely created a missing destination root, everything
        # after this point binds the exact directory that now exists.
        plan.dest_root_receipt = _refresh_root_receipt(binding)
        name = _audit_filename(prefix)
        temporary_name, temporary_fd = _reserve_temporary_file(
            binding.root_fd)
        try:
            with os.fdopen(
                os.dup(temporary_fd),
                "w",
                newline="",
                encoding="utf-8",
            ) as file_handle:
                writer = csv.writer(file_handle)
                writer.writerow([_csv_safe_cell(value) for value in header])
                for row in rows:
                    writer.writerow([_csv_safe_cell(value) for value in row])
                file_handle.flush()
                os.fsync(file_handle.fileno())
            _rename_noreplace_at(
                binding.root_fd, temporary_name, binding.root_fd, name)
            fsync_dirs(binding.root_fd)
        except BaseException:
            if _named_entry_matches(
                    binding.root_fd, temporary_name, temporary_fd):
                _remove_exact_named_file(
                    binding.root_fd, temporary_name, temporary_fd)
            raise
        finally:
            os.close(temporary_fd)
        return binding.path / name
    finally:
        binding.close()


def write_manifest(plan: MigrationPlan) -> Path:
    """Write the full reviewed plan beside the destination."""
    return _write_csv_for_plan(
        plan,
        "migration-manifest",
        ["status", "source_of_truth", "source", "destination", "reason"],
        [
            [
                entry.status,
                entry.source_of_truth,
                _csv_safe_cell(entry.source, path=True),
                _csv_safe_cell(entry.dest_rel, path=True)
                if entry.dest_rel is not None else "",
                entry.reason,
            ]
            for entry in plan.entries
        ],
    )


def _results_rows(result) -> list:
    companion_failures = sum(
        status == FAILED
        for _source, _destination, status, _reason
        in result.companion_outcomes
    )
    state = (
        "cancelled" if result.cancelled
        else "failed" if result.failed or companion_failures
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
    }
    rows = [[
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
    ]]
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
            ])
    return rows


def write_results_manifest(result: ExecResult, *, plan: MigrationPlan) -> Path:
    """Write every track and companion outcome beside the destination."""
    return _write_csv_for_plan(
        plan,
        "migration-results",
        [
            "record", "status", "source", "destination", "reason",
            "state", "restart",
        ],
        _results_rows(result),
    )
