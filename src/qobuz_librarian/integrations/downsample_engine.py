"""ffmpeg resampling engine for the downsample feature.

`downsample_dir` resamples a tree's high-rate FLACs in place; `scan_dir_for_hires`
reports what would shrink without touching anything. The higher-level discovery
(grouping into per-album review candidates) lives in library/downsample.py.

Targets (preserves integer-ratio family for clean math):
  88.2 / 176.4 / 352.8 kHz  →  44.1 kHz
  96   / 192   / 384   kHz  →  48   kHz

Quality settings:
  - Resampler: soxr at precision 28 if available, else swresample with
    filter_size=512, phase_shift=20 (very high quality).
  - Triangular high-pass dither (decorrelates quantization noise).
  - Source bit depth preserved (16-bit stays 16-bit, 24-bit stays 24-bit).
  - FLAC compression level 5 (default, lossless, fast encode).
"""
import hashlib
import os
import secrets
import shutil
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.file_exclusion import acquire_inode_write_exclusion
from qobuz_librarian.integrations.lyric_fetch import (
    _exchange_existing,
    _named_identity,
    _regular_identity,
    _unlink_held_name,
)
from qobuz_librarian.integrations.rip import flac_audio_offset
from qobuz_librarian.library import flac_cache


def _discard_unused_stash(kept_dir, log):
    """Drop a safety copy for a run that rewrote nothing.

    discard_redundant_backup re-proves every file byte-identical at its origin
    before removing anything, so this can only ever discard copies of files that
    were left untouched.
    """
    if kept_dir is None:
        return
    try:
        from qobuz_librarian.library.backup import discard_redundant_backup
        discard_redundant_backup(kept_dir)
    except OSError:
        log("  ⚠ downsample: couldn't clear the unused safety copy; "
            "it will expire on its own.")

# Downsampling needs ffmpeg to resample AND flac to verify the result. The
# overwrite is in-place and irreversible, so without the verifier there's no
# way to confirm a good encode replaced the only hi-res copy. Treat the
# feature as unavailable rather than overwrite unverified.
HAVE_DOWNSAMPLE = (shutil.which("ffmpeg") is not None
                   and shutil.which("flac") is not None)

# The container's music mount, used only as a fallback: every caller passes an
# explicit base_dir (the album/staging dir), so this is never the live root. It
# does NOT track cfg.MUSIC_ROOT (which defaults to ~/Music off-container), so a
# bare-metal caller must pass base_dir rather than rely on this default.
MUSIC_ROOT       = Path(os.environ.get("MUSIC_ROOT", "/music"))
# 1024 is enough to clear large ID3v2 preambles on the rare track that has one.
PROBE_BYTES      = 1024
try:
    RESAMPLE_WORKERS = max(1, int(os.environ.get("RESAMPLE_WORKERS", "4")))
except ValueError:
    RESAMPLE_WORKERS = 4  # a bad override must not poison import or crash the pool


def human(b):
    s = "-" if b < 0 else ""
    b = abs(b)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{s}{b:.1f} {u}"
        b /= 1024
    return f"{s}{b:.1f} PB"


_RESAMPLER_FILTER = None

_SOXR_FILTER = "aresample=resampler=soxr:precision=28:dither_method=triangular_hp"
_SWR_FILTER  = "aresample=filter_size=512:phase_shift=20:dither_method=triangular_hp"


def _soxr_available():
    """True when this ffmpeg can resample through libsoxr.

    Probed by running the soxr filter on a scrap of generated audio: that
    proves the filter loads and the precision/dither options take, which a
    build-config string wouldn't. ffmpeg has no flag that lists its
    resamplers, so there's nothing cheaper to query."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-f", "lavfi", "-i", "anullsrc=r=96000:cl=stereo",
             "-af", _SOXR_FILTER, "-ar", "48000", "-t", "0.05", "-f", "null", "-"],
            capture_output=True, timeout=10, stdin=subprocess.DEVNULL,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def detect_resampler_filter():
    """Pick the best resampler this ffmpeg has, as (-af filter, name).

    soxr at precision 28 when libsoxr is present, else swresample's wide
    polyphase filter. Cached after the first probe; ffmpeg's capabilities
    don't change within a run, and the per-album hook would otherwise probe
    once per album in a batch."""
    global _RESAMPLER_FILTER
    if _RESAMPLER_FILTER is None:
        _RESAMPLER_FILTER = ((_SOXR_FILTER, "soxr p28") if _soxr_available()
                             else (_SWR_FILTER, "swresample HQ"))
    return _RESAMPLER_FILTER


def parse_flac_info(data: bytes):
    """Return (sample_rate, bits_per_sample) from FLAC header bytes, or (0, 0)."""
    # STREAMINFO is always the first metadata block right after the fLaC marker
    # at offset 0, so anchor there instead of scanning. A leading ID3v2 tag (or any
    # bytes) that happens to contain "fLaC" would otherwise be read as the header
    # and yield a wrong rate/depth the metaflac backstop (which only fires on a 0)
    # never catches. A real leading-ID3 FLAC fails this and defers to metaflac.
    if not data.startswith(b"fLaC"):
        return 0, 0
    si = 8  # fLaC(4) + STREAMINFO block header(4)
    if len(data) < si + 14:
        return 0, 0
    sr = (data[si + 10] << 12) | (data[si + 11] << 4) | (data[si + 12] >> 4)
    bps = (((data[si + 12] & 0x1) << 4) | (data[si + 13] >> 4)) + 1
    return sr, bps


def read_local_bit_depth(path: Path, *, pass_fds=()) -> int:
    """Bit depth of a local FLAC. The 1 KB header byte-parse settles the common
    case in a single read; metaflac backstops a file whose STREAMINFO sits past
    the probe window (a leading ID3 tag would do it) so the resample never
    upconverts a 24-bit master to s32 off a misread. Returns 0 on failure.

    Mirrors read_sample_rate: a downsample run reads this per selected file, so
    the cheap byte read leads and the metaflac subprocess is the exception. A
    byte-parse that can't find the header returns 0 and defers to metaflac, so
    leading the cheap path costs no reliability."""
    try:
        with open(path, "rb") as f:
            data = f.read(PROBE_BYTES)
        _, bps = parse_flac_info(data)
    except OSError:
        return 0
    if bps:
        return bps
    try:
        completed = subprocess.run(
            ["metaflac", "--show-bps", str(path)],
            capture_output=True, text=True, timeout=10,
            pass_fds=tuple(pass_fds))
        if completed.returncode != 0:
            return 0
        out = completed.stdout.strip()
        return int(out) if out else 0
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired, ValueError):
        return 0


def _cached_sample_rate(path: Path) -> int:
    """Sample rate from the library scan's FLAC cache, or 0 when unavailable."""
    if not cfg.FLAC_CACHE_ENABLED:
        return 0
    try:
        cached = flac_cache.get(Path(path))
    except (AttributeError, OSError, TypeError):
        return 0
    if not isinstance(cached, dict):
        return 0
    try:
        return int(cached.get("sample_rate") or 0)
    except (TypeError, ValueError):
        return 0


def read_sample_rate(path: Path, *, pass_fds=()) -> int:
    """Sample rate of a local FLAC. The 1 KB header byte-parse settles the
    common case in a single read; metaflac backstops files whose STREAMINFO
    sits past the probe window (a leading ID3 tag would do it), so a hi-res
    file isn't silently dropped from the resample set. Returns 0 on failure.

    Inverse of read_local_bit_depth: this is the bulk path (one read per file
    across the whole library), so the cheap byte read leads and the subprocess
    is the exception, not the rule."""
    cached = _cached_sample_rate(path)
    if cached:
        return cached
    try:
        with open(path, "rb") as f:
            data = f.read(PROBE_BYTES)
        sr, _ = parse_flac_info(data)
    except OSError:
        return 0
    if sr:
        return sr
    try:
        completed = subprocess.run(
            ["metaflac", "--show-sample-rate", str(path)],
            capture_output=True, text=True, timeout=10,
            pass_fds=tuple(pass_fds))
        if completed.returncode != 0:
            return 0
        out = completed.stdout.strip()
        return int(out) if out else 0
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired, ValueError):
        return 0


def parse_flac_total_samples(data: bytes) -> int:
    """Return the 36-bit total_samples from FLAC STREAMINFO bytes, or 0.

    FLAC stores 0 for 'unknown/streaming', which this also returns as 0 so
    callers treat it as 'don't know' rather than 'zero-length'."""
    if not data.startswith(b"fLaC"):
        return 0
    si = 8  # fLaC(4) + STREAMINFO block header(4); anchored at offset 0
    if len(data) < si + 18:
        return 0
    return (((data[si + 13] & 0x0F) << 32)
            | (data[si + 14] << 24)
            | (data[si + 15] << 16)
            | (data[si + 16] << 8)
            | data[si + 17])


def read_total_samples(path: Path, *, pass_fds=()) -> int:
    """Total decoded sample count of a local FLAC, or 0 if unknown/unreadable.

    Same shape as read_sample_rate: the 1 KB header byte-parse settles the
    common case, and metaflac backstops a file whose STREAMINFO sits past the
    probe window (a leading ID3 tag). Used to confirm a resample didn't drop
    audio off a truncated source."""
    try:
        with open(path, "rb") as f:
            data = f.read(PROBE_BYTES)
        n = parse_flac_total_samples(data)
    except OSError:
        return 0
    if n:
        return n
    try:
        completed = subprocess.run(
            ["metaflac", "--show-total-samples", str(path)],
            capture_output=True, text=True, timeout=10,
            pass_fds=tuple(pass_fds))
        if completed.returncode != 0:
            return 0
        out = completed.stdout.strip()
        return int(out) if out else 0
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired, ValueError):
        return 0


def target_rate(sr):
    if sr in (88200, 176400, 352800):
        return 44100
    if sr in (96000, 192000, 384000):
        return 48000
    return None


def scan_dir_for_hires(directory):
    """List the high-sample-rate FLACs under `directory` (recursive) that the
    resampler would shrink, without touching anything.

    Returns ``{"hires": [{"path", "sr", "target", "size", "audio_size"}],
    "n_flac": int}``. ``audio_size`` is the file minus its metadata/art (which
    don't shrink), so a saving estimate scaled off it isn't inflated by a big
    embedded cover. The downsample scan uses this to build review candidates;
    downsample_dir runs the same probe before it resamples.
    """
    directory = Path(directory)
    hires = []
    n_flac = 0
    if not directory.is_dir():
        return {"hires": hires, "n_flac": 0}
    for p in directory.rglob("*"):
        # Case-insensitive: the rest of the app indexes *.FLAC too (suffix.lower),
        # so a case-sensitive glob would hide uppercase-extension files here.
        if (p.name.startswith(".") or not p.is_file()
                or p.suffix.lower() != ".flac"):
            continue
        n_flac += 1
        sr = read_sample_rate(p)
        rate = target_rate(sr)
        if not rate:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        offset = flac_audio_offset(p)
        audio_size = max(0, size - offset) if offset else size
        hires.append({"path": str(p), "sr": sr, "target": rate,
                      "size": size, "audio_size": audio_size})
    return {"hires": hires, "n_flac": n_flac}


def _decode_ok(path, *, pass_fds=()):
    """True only if the FLAC at `path` decodes cleanly (flac -t, frame-CRC +
    decode).

    The downsample overwrites the source in place with no re-download to fall
    back on, so anything that leaves the encode unverified, including a decode failure, a
    timeout, or a missing/unusable flac binary (HAVE_DOWNSAMPLE already requires
    one, but it could vanish mid-run on a network mount), returns False, and the
    encode is discarded rather than allowed to replace the original."""
    try:
        r = subprocess.run(["flac", "-t", "-s", str(path)],
                           capture_output=True, timeout=300,
                           stdin=subprocess.DEVNULL,
                           pass_fds=tuple(pass_fds))
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


# Where a would-clip file is pulled down to. A downsample never lifts the
# ceiling: only files whose resample overshoots full scale are attenuated, and
# only to here, so everything else stays bit-untouched.
_TARGET_PEAK_DBFS = -1.0


def _resampled_peak_dbfs(src, af_filter, rate, *, pass_fds=()):
    """Peak level (dBFS) the resampled signal reaches, on a float render. soxr's
    reconstruction can push a hot (loud) source past full scale; the integer
    FLAC written below would hard-clip that overshoot into audible distortion.
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(src),
             "-map", "0:a", "-af", f"{af_filter},astats=metadata=1:reset=0",
             "-ar", str(rate), "-c:a", "pcm_f32le", "-f", "null", "-"],
            capture_output=True, timeout=600, stdin=subprocess.DEVNULL,
            pass_fds=tuple(pass_fds))
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    peaks = []
    for line in r.stderr.decode(errors="replace").splitlines():
        if "Peak level dB" in line:
            try:
                peaks.append(float(line.split("Peak level dB:")[1]))
            except (ValueError, IndexError):
                continue
    return max(peaks) if peaks else None


# Resample output is written to an unpredictable, exclusively-created temp
# beside the source, then atomically exchanged with the exact held master. The
# dot keeps the library scanner from indexing an in-flight encode.
_TMP_PREFIX = ".compress-"


class _BoundSource:
    """One regular file beneath a fully held, no-follow absolute root chain."""

    def __init__(self, path: Path, owned_root: Path):
        nofollow = getattr(os, "O_NOFOLLOW", None)
        directory = getattr(os, "O_DIRECTORY", None)
        if nofollow is None or directory is None:
            raise OSError("safe no-follow file operations are unavailable")

        self.path = Path(os.path.abspath(os.fspath(path)))
        self.root = Path(os.path.abspath(os.fspath(owned_root)))
        try:
            relative = self.path.relative_to(self.root)
        except ValueError:
            raise OSError(
                f"{self.path}: source is outside the downsample root") from None
        if not relative.parts or any(
                part in ("", ".", "..") for part in relative.parts):
            raise OSError(f"{self.path}: invalid source path")

        root_parts = self.root.parts
        if not root_parts or root_parts[0] != os.path.sep:
            raise OSError(f"{self.root}: invalid downsample root")
        self._directory_fds = []
        self._directory_names = []
        self.track_fd = None
        directory_flags = os.O_RDONLY | directory | nofollow
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            filesystem_root_fd = os.open(os.path.sep, directory_flags)
            self._directory_fds.append(filesystem_root_fd)
            for part in (*root_parts[1:], *relative.parts[:-1]):
                child_fd = os.open(
                    part,
                    directory_flags,
                    dir_fd=self._directory_fds[-1],
                )
                if not self._directory_entry_matches(
                        self._directory_fds[-1], part, child_fd):
                    os.close(child_fd)
                    raise OSError(f"{self.path}: directory chain changed")
                self._directory_names.append(part)
                self._directory_fds.append(child_fd)

            track_flags = (
                os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0))
            track_flags |= getattr(os, "O_CLOEXEC", 0)
            self.track_fd = os.open(
                relative.parts[-1],
                track_flags,
                dir_fd=self._directory_fds[-1],
            )
            self.identity = _regular_identity(os.fstat(self.track_fd))
            if (
                self.identity is None
                or not self.chain_is_named()
                or self.named_identity() != self.identity
            ):
                raise OSError(f"{self.path}: source identity changed")
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _directory_entry_matches(parent_fd, name, descriptor) -> bool:
        try:
            held = os.fstat(descriptor)
            named = os.stat(
                name, dir_fd=parent_fd, follow_symlinks=False)
            return (
                stat.S_ISDIR(held.st_mode)
                and stat.S_ISDIR(named.st_mode)
                and held.st_dev == named.st_dev
                and held.st_ino == named.st_ino
            )
        except OSError:
            return False

    @property
    def parent_fd(self) -> int:
        return self._directory_fds[-1]

    @property
    def name(self) -> str:
        return self.path.name

    def chain_is_named(self) -> bool:
        try:
            held_root = os.fstat(self._directory_fds[0])
            named_root = os.stat(os.path.sep, follow_symlinks=False)
            if (
                not stat.S_ISDIR(held_root.st_mode)
                or not stat.S_ISDIR(named_root.st_mode)
                or held_root.st_dev != named_root.st_dev
                or held_root.st_ino != named_root.st_ino
            ):
                return False
            for index, name in enumerate(self._directory_names, start=1):
                if not self._directory_entry_matches(
                        self._directory_fds[index - 1],
                        name,
                        self._directory_fds[index],
                ):
                    return False
        except (OSError, IndexError):
            return False
        return True

    def named_identity(self):
        try:
            return _named_identity(self.parent_fd, self.name)
        except OSError:
            return None

    def exact_track_is_named(self) -> bool:
        if self.track_fd is None or not self.chain_is_named():
            return False
        try:
            return (
                _regular_identity(os.fstat(self.track_fd)) == self.identity
                and self.named_identity() == self.identity
            )
        except OSError:
            return False

    def close(self) -> None:
        if self.track_fd is not None:
            try:
                os.close(self.track_fd)
            except OSError:
                pass
            self.track_fd = None
        for descriptor in reversed(self._directory_fds):
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._directory_fds.clear()


def _descriptor_path(descriptor: int) -> Path:
    return Path(f"/proc/self/fd/{descriptor}")


def _make_encode_temp(parent_fd: int):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("safe no-follow file operations are unavailable")
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | nofollow
    flags |= getattr(os, "O_CLOEXEC", 0)
    for _ in range(16):
        name = f"{_TMP_PREFIX}{secrets.token_hex(16)}.flac"
        try:
            return name, os.open(name, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            continue
    raise FileExistsError("couldn't reserve a downsample temporary file")


def _exact_temp_is_named(parent_fd: int, name: str, descriptor: int,
                         expected=None) -> bool:
    try:
        frozen = expected or _regular_identity(os.fstat(descriptor))
        return (
            frozen is not None
            and _regular_identity(os.fstat(descriptor)) == frozen
            and _named_identity(parent_fd, name) == frozen
        )
    except OSError:
        return False


def _reopen_encode_temp_readonly(parent_fd: int, name: str, descriptor: int):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("safe no-follow file operations are unavailable")
    expected = _regular_identity(os.fstat(descriptor))
    if expected is None or not _exact_temp_is_named(
            parent_fd, name, descriptor, expected):
        raise OSError("resampled output changed while it was being opened")
    flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    readonly_fd = os.open(name, flags, dir_fd=parent_fd)
    if not _exact_temp_is_named(parent_fd, name, readonly_fd, expected):
        os.close(readonly_fd)
        raise OSError("resampled output changed while it was being opened")
    return readonly_fd, expected


def _sha256_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)
        offset += len(chunk)


def _receipt_matches_source(binding, receipt, exclusion) -> bool:
    if not isinstance(receipt, dict) or not exclusion.intact():
        return False
    try:
        value = os.fstat(binding.track_fd)
        identity = receipt.get("identity")
        expected_identity = (
            stat.S_IFMT(value.st_mode), int(value.st_dev), int(value.st_ino))
        if (
            not isinstance(identity, (tuple, list))
            or tuple(identity) != expected_identity
            or receipt.get("size") != int(value.st_size)
            or receipt.get("mtime_ns") != int(value.st_mtime_ns)
            or receipt.get("changed_ns") != int(value.st_ctime_ns)
            or not isinstance(receipt.get("sha256"), str)
            or len(receipt["sha256"]) != 64
            or not binding.exact_track_is_named()
        ):
            return False
        digest = _sha256_fd(binding.track_fd)
        after = os.fstat(binding.track_fd)
        return (
            exclusion.intact()
            and binding.exact_track_is_named()
            and _regular_identity(after) == binding.identity
            and digest == receipt["sha256"]
        )
    except (OSError, TypeError, ValueError):
        return False


def _encode_opts_for_bps(bps, af_filter):
    """ffmpeg options that keep the output FLAC at the source bit depth. Returns
    (af, sample_fmt, depth_args).
    """
    if bps == 16:
        return af_filter, "s16", []
    if bps in (8, 24, 32):
        return (f"{af_filter},aformat=sample_fmts=s32", "s32",
                ["-bits_per_raw_sample", str(bps)])
    return af_filter, "s32", []


def resample_one(rel, sr, rate, af_filter, *, base_dir=None,
                 expected_source_receipt=None):
    """Resample one file. Returns (rel, sr, rate, saved_bytes, error_msg).

    base_dir defaults to MUSIC_ROOT. Pass an alternate root (e.g.
    STAGING_DIR) when operating on files that have not been imported
    onto the canonical music tree yet.
    """
    root = Path(base_dir or MUSIC_ROOT)
    src = root / rel
    binding = None
    exclusion = None
    temp_exclusion = None
    temp_fd = None
    temp_name = None
    try:
        binding = _BoundSource(src, root)
        exclusion = acquire_inode_write_exclusion(binding.track_fd)
        if exclusion is None:
            return (rel, sr, rate, None,
                    "source is open for writing or cannot be protected; "
                    "left the original untouched")
        if (
            expected_source_receipt is not None
            and not _receipt_matches_source(
                binding, expected_source_receipt, exclusion)
        ):
            return (rel, sr, rate, None,
                    "source no longer matches its kept original; "
                    "left it untouched")

        st = os.fstat(binding.track_fd)
        in_size = st.st_size
        src_mode = stat.S_IMODE(st.st_mode)
        source_path = _descriptor_path(binding.track_fd)
        source_fds = (binding.track_fd,)

        held_sr = read_sample_rate(source_path, pass_fds=source_fds)
        if held_sr != sr or target_rate(held_sr) != rate:
            return (rel, sr, rate, None,
                    "source sample rate changed after the scan; "
                    "left the original untouched")

        bps = read_local_bit_depth(source_path, pass_fds=source_fds)
        # An unreadable source bit depth (bps == 0) can't be pinned. The
        # encode would default to 32-bit and inflate a 24-bit master, and
        # can't be re-verified afterward, so refuse rather than overwrite the
        # master in place. A downsample has no re-download to fall back on; an
        # un-shrunk file is far cheaper than a silently altered master.
        if not bps:
            return (rel, sr, rate, None,
                    "couldn't read the source bit depth; left the original untouched")
        af, sample_fmt, depth_args = _encode_opts_for_bps(bps, af_filter)
        # Keep the resample from clipping a hot master: soxr can overshoot
        # full scale on loud/brickwalled sources, which the integer encode
        # would hard-clip into audible distortion. Measure the resampled peak;
        # if it would exceed full scale, attenuate the source by just enough
        # to land it at _TARGET_PEAK_DBFS (inaudible, and only on files that
        # would clip; everything else stays bit-untouched).
        enc_af = af
        _peak = _resampled_peak_dbfs(
            source_path, af_filter, rate, pass_fds=source_fds)
        if _peak is None:
            return (rel, sr, rate, None,
                    "couldn't verify the resampled peak; left the original untouched")
        if _peak is not None and _peak > 0.0:
            enc_af = f"volume={_TARGET_PEAK_DBFS - _peak:.3f}dB,{af}"
        if not exclusion.intact() or not binding.exact_track_is_named():
            return (rel, sr, rate, None,
                    "source changed while it was being checked; "
                    "left the current file untouched")

        temp_name, temp_fd = _make_encode_temp(binding.parent_fd)
        temp_path = _descriptor_path(temp_fd)

        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-i", str(source_path),
                # Map every input stream so all embedded PICTURE blocks
                # (front+back cover) survive; ffmpeg's default selection keeps
                # only one video stream and would drop the rest.
                "-map", "0",
                "-af", enc_af,
                "-ar", str(rate),
                "-sample_fmt", sample_fmt,
                *depth_args,
                "-c:a", "flac",
                # Copy attached art bit-for-bit. Without this the FLAC muxer's
                # default video codec (PNG) transcodes the embedded JPEG cover,
                # inflating the file (a multi-MB Qobuz cover can wipe out the
                # audio savings and make the output net-larger).
                "-c:v", "copy",
                "-map_metadata", "0",
                "-f", "flac",
                "-y", str(temp_path),
            ],
            check=True,
            capture_output=True,
            # A hung NFS/FUSE mount must not freeze the worker forever; cap the
            # encode. Generous even for a long 24/192 file on slow hardware.
            timeout=600,
            pass_fds=(binding.track_fd, temp_fd),
        )
        readonly_fd, temp_identity = _reopen_encode_temp_readonly(
            binding.parent_fd, temp_name, temp_fd)
        writable_fd = temp_fd
        temp_fd = readonly_fd
        os.close(writable_fd)
        temp_path = _descriptor_path(temp_fd)
        temp_exclusion = acquire_inode_write_exclusion(temp_fd)
        if (
            temp_exclusion is None
            or temp_identity is None
            or not _exact_temp_is_named(
                binding.parent_fd, temp_name, temp_fd, temp_identity)
            or not exclusion.intact()
            or not temp_exclusion.intact()
            or not binding.exact_track_is_named()
        ):
            return (rel, sr, rate, None,
                    "source or resampled output changed during encoding; "
                    "left the current file untouched")
        out_size = temp_identity["size"]
        # Verify the encode decodes before it overwrites the source. A
        # downsample has no re-download to fall back on, so a corrupt encode
        # must never replace a good original.
        if not _decode_ok(temp_path, pass_fds=(temp_fd,)):
            return (rel, sr, rate, None, "resampled file failed verification")
        # Never let a resample silently change the bit depth. A 24-bit master
        # must not come back 32-bit, nor an 8-bit file get padded to 24. The
        # source depth is known here (an unreadable one was refused above), so
        # this always runs. Original untouched on a mismatch.
        out_bps = read_local_bit_depth(temp_path, pass_fds=(temp_fd,))
        if out_bps != bps:
            return (rel, sr, rate, None,
                    f"resampled to {out_bps}-bit, expected {bps}-bit; "
                    "left the original untouched")
        # Verify the resample kept the whole stream. ffmpeg exits 0 on a
        # truncated or mid-file-corrupt source, emitting only the decodable
        # prefix, and flac -t then passes on that short output, so a damaged
        # hi-res master (the exact file class the repair feature exists to
        # catch) could be silently replaced by a shortened encode, laundering
        # the damage out of every later integrity probe.
        in_samples = read_total_samples(source_path, pass_fds=source_fds)
        out_samples = read_total_samples(temp_path, pass_fds=(temp_fd,))
        if not (in_samples and out_samples):
            # A 0 = STREAMINFO 'unknown' on either side means the length can't
            # be verified, and a truncated-but-decodable encode would slip
            # through unnoticed. Refuse and keep the original; an un-shrunk
            # file is a far cheaper outcome than a silently-truncated master
            # with no recovery path.
            return (rel, sr, rate, None,
                    "couldn't verify resampled length (source/output STREAMINFO "
                    "reports unknown sample count); left the original untouched")
        expected = in_samples * rate / sr
        # Cap the relative term at ~1s of output. expected*0.005 alone scales
        # with length (~21s on a 70-min master), which would let a long source
        # lose many seconds and still pass, the opposite of this gate's job.
        tol = max(rate // 10, min(expected * 0.005, rate))
        if abs(out_samples - expected) > tol:
            return (rel, sr, rate, None,
                    f"resampled to {out_samples / rate:.1f}s, expected "
                    f"~{in_samples / sr:.1f}s; left the original untouched "
                    "(source may be truncated)")
        # With art copied bit-for-bit a real downsample always shrinks the file;
        # if it somehow didn't, replacing the source only churns it (and a
        # larger output would drive saved_bytes negative), so keep the original.
        if out_size >= in_size:
            return (rel, sr, rate, None,
                    "resampled output not smaller than source; "
                    "left the original untouched")
        # The exclusive temp starts at 0o600; carry the source's mode across the
        # swap so a downsample doesn't quietly tighten a 0o644 library file to
        # owner-only (an annoyance on shared/NAS libraries).
        try:
            os.fchmod(temp_fd, src_mode)
        except OSError:
            pass
        temp_identity = _regular_identity(os.fstat(temp_fd))
        if (
            temp_identity is None
            or not _exact_temp_is_named(
                binding.parent_fd, temp_name, temp_fd, temp_identity)
            or not exclusion.intact()
            or not temp_exclusion.intact()
            or not binding.exact_track_is_named()
        ):
            return (rel, sr, rate, None,
                    "source or resampled output changed before replacement; "
                    "left the current file untouched")
        # The swap overwrites the only lossless master, so a flush that
        # genuinely fails (ENOSPC/EIO, not a mount that can't fsync) must
        # refuse the replace: the encode may exist only in the page cache.
        from qobuz_librarian.library.backup import _fsync
        if not _fsync(temp_path):
            return (rel, sr, rate, None,
                    "resampled copy couldn't be flushed to disk; "
                    "left the original untouched")
        if (
            not exclusion.intact()
            or not temp_exclusion.intact()
            or not binding.exact_track_is_named()
            or not _exact_temp_is_named(
                binding.parent_fd, temp_name, temp_fd, temp_identity)
        ):
            return (rel, sr, rate, None,
                    "source or resampled output changed before replacement; "
                    "left the current file untouched")

        # _exchange_existing acquires its own fresh lease at the final identity
        # gate. Release this long-held encode lease first; any write landing in
        # the narrow handoff changes the full source identity and is refused.
        exclusion.close()
        exclusion = None
        _exchange_existing(
            binding.parent_fd,
            temp_name,
            binding.name,
            temp_fd,
            binding.track_fd,
            expected_identity=binding.identity,
            new_identity=temp_identity,
            post_exchange_guard=binding.chain_is_named,
        )
        # The encode itself was flushed above, so rename atomicity leaves a
        # valid file either way, but a directory flush that genuinely fails
        # means the swap may quietly revert on a crash while the run reports
        # it done. Nothing here can be undone; say so instead of staying quiet.
        from qobuz_librarian.library.backup import _fsync
        if not _fsync(_descriptor_path(binding.parent_fd)):
            return (rel, sr, rate, in_size - out_size,
                    "resampled, but the folder couldn't be flushed to disk; "
                    "the swap may not survive a power loss; check the drive")
        return (rel, sr, rate, in_size - out_size, None)
    except subprocess.TimeoutExpired:
        return (rel, sr, rate, None,
                "ffmpeg timed out (slow storage?); left the original untouched")
    except subprocess.CalledProcessError as e:
        err = (e.stderr.decode(errors="replace")[:200] if e.stderr else "").strip()
        return (rel, sr, rate, None, err)
    except Exception as e:
        return (rel, sr, rate, None, str(e))
    finally:
        try:
            if exclusion is not None:
                exclusion.close()
        finally:
            try:
                # A mutable filename is never enough ownership evidence.
                # Remove only the exact held temp this operation created;
                # after a successful swap its descriptor names the published
                # output, not temp_name, so this is safely a no-op.
                if (
                    temp_fd is not None
                    and binding is not None
                    and temp_name is not None
                ):
                    try:
                        identity = _regular_identity(os.fstat(temp_fd))
                        if identity is not None:
                            _unlink_held_name(
                                binding.parent_fd,
                                temp_name,
                                temp_fd,
                                identity,
                                write_exclusion=temp_exclusion,
                            )
                    except OSError:
                        pass
            finally:
                try:
                    if temp_exclusion is not None:
                        temp_exclusion.close()
                finally:
                    try:
                        if temp_fd is not None:
                            os.close(temp_fd)
                    finally:
                        if binding is not None:
                            binding.close()


def downsample_dir(directory, *, verbose=True, base_dir=None, log=print,
                   keep_originals=False, cancel_check=None):
    """Resample any high-sample-rate FLACs inside `directory` (recursive). Probes
    each file's sample rate, resamples the ones above CD rate, and leaves the
    rest untouched. `base_dir` is the root paths are taken relative to
    (defaults to MUSIC_ROOT); pass the album/staging dir when the files aren't
    on the canonical music tree yet.
    """
    directory = Path(directory)
    if not directory.exists() or not directory.is_dir():
        return {"resampled": 0, "errors": 0, "saved_bytes": 0}

    _bd = base_dir or MUSIC_ROOT
    af_filter, _ = detect_resampler_filter()

    candidates = []
    for p in directory.rglob("*"):
        if p.name.startswith(".") or not p.is_file() or p.suffix.lower() != ".flac":
            continue
        try:
            rel = str(p.relative_to(_bd))
        except ValueError:
            # outside _bd; resample_one assumes _bd/rel
            continue
        sr = read_sample_rate(p)
        rate = target_rate(sr)
        if rate:
            candidates.append((rel, sr, rate))

    if not candidates:
        return {"resampled": 0, "errors": 0, "saved_bytes": 0}

    if verbose:
        log(f"  ⇳ downsample: {len(candidates)} file(s) in {directory.name}")

    # A Stop can land while this album waits its turn for the staging lock;
    # the poll below only runs as encodes finish, so without an entry check
    # the album would start rewriting the moment the lock arrives. Nothing
    # has been touched yet. Report a clean cancel before even stashing.
    if cancel_check is not None and cancel_check():
        return {"resampled": 0, "errors": 0, "saved_bytes": 0,
                "cancelled": True}

    n_uncopied = 0
    source_receipts = {}
    kept_dir = None
    if keep_originals:
        from qobuz_librarian.library.backup import stash_downsample_originals
        kept_dir, copied, receipts = stash_downsample_originals(
            [_bd / rel for rel, _, _ in candidates],
            directory,
            include_identity_receipts=True,
        )
        protected = [
            c for c in candidates
            if _bd / c[0] in copied
            and isinstance(receipts.get(_bd / c[0]), dict)
        ]
        source_receipts = {
            c[0]: receipts[_bd / c[0]]
            for c in protected
        }
        n_uncopied = len(candidates) - len(protected)
        if n_uncopied:
            log(f"  ⚠ downsample: {n_uncopied} file(s) have no safety copy; "
                "left untouched.")
        candidates = protected
        # Copying large masters can take long enough for Stop to arrive after
        # the entry check above. The copies are only preparation, not authority
        # to begin an irreversible rewrite after cancellation.
        if cancel_check is not None and cancel_check():
            _discard_unused_stash(kept_dir, log)
            return {
                "resampled": 0,
                "errors": n_uncopied,
                "saved_bytes": 0,
                "cancelled": True,
            }
        if not candidates:
            # Nothing to rewrite after all. The copies we just took are not a
            # safety net for anything, so don't leave them parked as a 7-day
            # "undo" for a run that changed nothing.
            _discard_unused_stash(kept_dir, log)
            return {"resampled": 0, "errors": n_uncopied, "saved_bytes": 0}

    saved_total = 0
    errors = 0
    resampled = 0
    flush_warnings = 0

    cancelled = False
    tallied = set()

    def _tally(fut):
        nonlocal errors, resampled, saved_total, flush_warnings
        tallied.add(fut)
        rel, _sr, _rate, saved, err = fut.result()
        if err is not None and saved is not None:
            # The rewrite finished; only the directory flush failed, so the
            # file on disk IS the resampled copy. Counting it as untouched
            # would leave the album unmarked as capped and the tally claiming
            # nothing changed while the lossless master is already gone.
            resampled += 1
            saved_total += saved
            flush_warnings += 1
            if verbose:
                log(f"  ⚠ {Path(rel).name}: {err}")
        elif err is not None:
            errors += 1
            if verbose:
                log(f"  ✗ {Path(rel).name}: {err}")
        else:
            resampled += 1
            saved_total += saved

    with ThreadPoolExecutor(max_workers=RESAMPLE_WORKERS) as ex:
        futs = {}
        for rel, sr, rate in candidates:
            kwargs = {"base_dir": _bd}
            if keep_originals:
                kwargs["expected_source_receipt"] = source_receipts[rel]
            future = ex.submit(
                resample_one, rel, sr, rate, af_filter, **kwargs)
            futs[future] = rel
        try:
            for fut in as_completed(futs):
                _tally(fut)
                if (cancel_check is not None and not cancelled
                        and cancel_check()):
                    # Must BREAK here, not keep iterating: futures cancelled
                    # by shutdown(cancel_futures=True) never notify
                    # as_completed's waiters (no worker ever dequeues them),
                    # so the iterator would wait on them forever. The context
                    # exit below waits out the in-flight encodes; they're
                    # tallied after the block so the counts reflect disk state.
                    cancelled = True
                    ex.shutdown(wait=False, cancel_futures=True)
                    if verbose:
                        log("  ⏹ downsample: stop requested; finishing the "
                            "in-flight track(s), discarding the rest")
                    break
        except KeyboardInterrupt:
            # Stop promptly: discard not-yet-started encodes instead of letting
            # the context manager block on shutdown(wait=True) for the whole queue.
            ex.shutdown(wait=False, cancel_futures=True)
            raise
    if cancelled:
        for fut in futs:
            if fut not in tallied and fut.done() and not fut.cancelled():
                _tally(fut)
        if all(f.done() and not f.cancelled() for f in futs):
            # The stop arrived after the final track had already finished;
            # every candidate was processed and tallied, nothing was cut
            # short. Reporting "cancelled" here would leave the album
            # uncounted and uncapped, so a fully-downsampled album would be
            # offered all over again.
            cancelled = False

    errors_total = errors + n_uncopied
    needs_attention = bool(errors_total or flush_warnings or cancelled)
    if verbose and (resampled or needs_attention):
        if needs_attention:
            caveats = []
            if errors_total:
                caveats.append(f"{errors_total} failed")
            if flush_warnings:
                caveats.append(f"{flush_warnings} flush warning")
            if cancelled:
                caveats.append("stopped early")
            log(
                f"  ⚠ downsample needs attention: {resampled} resampled, "
                f"saved {human(saved_total)}; {'; '.join(caveats)}"
            )
        else:
            log(f"  ✓ downsample: {resampled} resampled, "
                f"saved {human(saved_total)}")
    # The stash is taken up front, before a single file is encoded, so the
    # intent to rewrite is not proof of one. Claim the kept originals, and
    # keep them, only once something actually was rewritten.
    if kept_dir is not None:
        if resampled:
            if verbose:
                log(f"  ⤷ originals kept {cfg.UPGRADE_BACKUP_RETENTION_DAYS} "
                    f"day(s): restore from Settings → Diagnostics")
        else:
            _discard_unused_stash(kept_dir, log)

    return {"resampled": resampled,
            "errors": errors_total,
            "saved_bytes": saved_total,
            "flush_warnings": flush_warnings,
            "cancelled": cancelled}
