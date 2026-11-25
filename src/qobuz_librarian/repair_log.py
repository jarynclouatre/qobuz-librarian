"""Repair log and ISRC-based truncation scanner.

Two non-obvious bits of behaviour worth preserving:

- `scan_dir_for_isrc_repairs` flags a file as truncated when it is both more
  than 30 s short of its Qobuz duration AND either under 85% of it or more than
  60 s short. The 30 s floor and the 85% ratio keep brief tracks / live
  recordings from false-flagging on a small trim; the 60 s absolute cap stops a
  long track (a 20-minute classical movement) from hiding a minute of missing
  music behind the percentage.
- `append_repair_log` replaces pipe characters in artist/album/title
  fields with slashes (AC|DC → AC/DC) so the pipe-delimited log format
  stays unambiguously parseable.
"""
import fcntl
import functools
import hashlib
import os
import re
import shutil
import stat
import threading
import time
from collections import Counter
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost, QobuzUnavailable
from qobuz_librarian.api.search import find_qobuz_tracks_by_isrc, get_track
from qobuz_librarian.integrations.rip import flac_audio_offset, flac_audio_ok
from qobuz_librarian.library import catalog, repair_cache, scanner
from qobuz_librarian.library.scanner import iter_tree_no_symlinks, parse_track_num
from qobuz_librarian.library.tags import canonical_track_title
from qobuz_librarian.ui_cli.colors import C, fmt
from qobuz_librarian.ui_cli.logging import log

_lookup_lock = threading.Lock()
_last_lookup = 0.0


def _throttle_lookup():
    """Pace live Qobuz ISRC lookups across all repair-scan workers so a cold deep
    sweep (one lookup per track over several workers) doesn't burst the account.
    Only live lookups call this; cache hits skip it. 0 interval disables it."""
    global _last_lookup
    interval = float(cfg.REPAIR_LOOKUP_MIN_INTERVAL)
    if interval <= 0:
        return
    with _lookup_lock:
        wait = interval - (time.time() - _last_lookup)
        if wait > 0:
            time.sleep(wait)
        _last_lookup = time.time()


def _qobuz_tracks_by_isrc(isrc, token):
    """Cache the search hits. Albums sharing an ISRC can need different cuts."""
    cached = repair_cache.get_tracks(isrc)
    if cached is not None:
        return cached
    _throttle_lookup()
    tracks = find_qobuz_tracks_by_isrc(isrc, token)
    if tracks:
        repair_cache.put_tracks(isrc, tracks)
    return tracks


def truncated_tracks_after_download(album_dir, token, tracks=None):
    """Re-verify a freshly downloaded album's track lengths against Qobuz and
    return the tracks that came up short. The download already drops files that
    won't decode, so this compares lengths only, and only for the Qobuz
    `tracks` a gap fill fetched (every file when that is not given). A clean
    truncation (decodes fine, header rewritten to the short length) only shows
    against the real recording length. Best-effort: a lookup hiccup returns an
    empty list so a finished download is never failed by the check. AuthLost /
    QobuzUnavailable propagate, though: an expired token or an outage means
    the lengths weren't actually verified, so a caller must be able to tell
    that apart from a clean album rather than read it as 'all fine'."""
    isrcs = {
        str(track.get("isrc") or "").replace("-", "").upper().strip()
        for track in tracks or ()
        if isinstance(track, dict)
    }
    if not tracks or "" in isrcs:
        isrcs = None
    try:
        scan = scan_dir_for_isrc_repairs(
            album_dir, token, deep=True, only_isrcs=isrcs, decode=False)
    except (AuthLost, QobuzUnavailable):
        raise
    except Exception:
        return []
    return scan.get("verified_truncated", [])


def warn_if_download_truncated(album_dir, token, label, tracks=None):
    """Run the post-download truncation recheck and log a Repair nudge for any
    track shorter than its real Qobuz recording. Advisory only: it never alters
    or fails the download. A clean run logs nothing; a lost token or a Qobuz
    outage says so plainly (so an unverified album isn't mistaken for a clean
    one) instead of silently reporting nothing. Returns the short-track list."""
    try:
        short = truncated_tracks_after_download(album_dir, token, tracks)
    except (AuthLost, QobuzUnavailable):
        log.info(fmt(C.YELLOW,
            f"  ⚠  {label or 'this album'} imported, but its track lengths "
            "couldn't be verified against Qobuz (auth lost or Qobuz "
            "unavailable). Re-run Repair once Qobuz is reachable."))
        return []
    if short:
        n = len(short)
        log.info(fmt(C.YELLOW,
            f"  ⚠  {label or 'this album'}: {n} track{'s' if n != 1 else ''} "
            "shorter than the Qobuz length. Run Repair to refill."))
    return short

# Lossless FLAC compresses music to roughly 0.40-0.65 of raw PCM; even very
# compressible material rarely lands below ~0.30. A file whose *audio portion*
# (file size minus metadata) is under 15% of the uncompressed-equivalent size
# for the duration STREAMINFO claims is almost certainly truncated: the
# header survives tail damage and lies about how much audio is actually
# present.
_BYTE_SIZE_TRUNCATED_RATIO = 0.15

# Verifying every FLAC frame's CRC costs a full read per file. Worth it for an
# explicit repair scan: the cheap size+duration gates can't see small tail-
# truncations (10 kB on a 100 MB file) or middle-zero damage, which is exactly
# the failure mode interrupted-copy corruption produces.


def _flac_decode_ok(path, *, descriptor=None):
    """End-to-end FLAC integrity probe via ``flac -t`` (frame-CRC + decode).
    Returns False only on a real decode error (CRC mismatch, broken header,
    premature EOF). A missing file or a missing flac tool returns True so the
    scanner doesn't fabricate verified_truncated entries it can't stand behind.
    """
    if descriptor is None and (not path or not os.path.exists(path)):
        return True
    ok = flac_audio_ok(Path(path), descriptor=descriptor)
    return True if ok is None else ok


_MISMATCHED_DAMAGE_DIAGNOSTIC = (
    "damaged, but the recording or edition could not be confirmed for a "
    "single-track refill")


_BRACKET_LABEL_RE = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]")


def _repair_title_parts(title, version=""):
    labels = {canonical_track_title(a or b) for a, b in _BRACKET_LABEL_RE.findall(title or "")}
    packaging = {label for label in labels if catalog._PACKAGING_LABEL_RE.fullmatch(label)}
    return catalog._edition_title_parts(title, version, known_versions=packaging)


def _same_recording(local, track, album_dir):
    """A shared ISRC can name two cuts, or even two different songs."""
    version = "" if catalog._version_in_title(track) else track.get("version")
    title, track_labels = _repair_title_parts(track.get("title"), version)
    held_title, held = _repair_title_parts(local.get("title"))
    if not title or title != held_title:
        return False
    album = track.get("album") or {}
    _, album_labels = catalog._edition_title_parts(album.get("title"), album.get("version"))
    _, local_labels = catalog._edition_title_parts(local.get("album"))
    held |= local_labels
    for match in _BRACKET_LABEL_RE.finditer(album_dir.name):
        label = match.group(1) or match.group(2)
        if catalog._VERSION_LABEL_RE.search(label):
            held |= catalog._label_parts(label)
    held = catalog._recording_labels(held)
    track_cut = catalog._recording_labels(track_labels)
    album_cut = catalog._recording_labels(album_labels)
    if held == track_cut | album_cut:
        return True
    # Qobuz also uses a bare album-level "Remastered" on otherwise unlabelled files.
    return not held and not track_cut and album_cut in ({"remaster"}, {"remastered"})


def _matching_repair_track(local, tracks, album_dir):
    artists = {canonical_track_title(name) for name in local.get("artists") or ()} - {""}
    matches = []
    for track in tracks:
        album = track.get("album") or {}
        performer = track.get("performer") or album.get("artist") or {}
        name = canonical_track_title(performer.get("name"))
        if artists and name not in artists:
            continue
        if _same_recording(local, track, album_dir):
            matches.append(track)
    if len(matches) > 1:
        title, _ = catalog._edition_title_parts(local.get("album"))
        matches = [t for t in matches if title and
                   catalog._edition_title_parts((t.get("album") or {}).get("title"))[0] == title]
    if len(matches) > 1:
        matches = [t for t in matches if
                   parse_track_num(t.get("track_number")) == local.get("tracknumber")
                   and parse_track_num(t.get("media_number") or 1) == local.get("discnumber")]
    return matches[0] if len(matches) == 1 else None


def refill_recording(entry, album_dir, album, token):
    """Check the selected release and the current track before moving the original."""
    with _HeldRepairSource(album_dir, entry["path"]) as source:
        local = _read_held_audio_meta(source)
        isrc = local.get("isrc")
        if not isrc or isrc != catalog._norm_isrc(entry.get("isrc")):
            return None
        release = {key: album.get(key) for key in ("id", "title", "version", "artist")}
        tracks = [{**t, "album": release} for t in (album.get("tracks") or {}).get("items") or []
                  if catalog._norm_isrc(t.get("isrc")) == isrc]
        choice = _matching_repair_track(local, tracks or [entry["qobuz_track"]], album_dir)
        if not choice or not choice.get("id"):
            return None
        track = get_track(choice["id"], token)
        if (not track
                or catalog._norm_isrc(track.get("isrc")) != isrc
                or tracks and str((track.get("album") or {}).get("id")) != str(album.get("id"))
                or not source.intact()):
            return None
        return _matching_repair_track(local, [track], album_dir)


def _stat_identity(value):
    return (
        int(value.st_dev),
        int(value.st_ino),
        stat.S_IFMT(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


class _HeldRepairSource:
    """One no-follow album file kept open for its whole diagnosis."""

    def __init__(self, album_dir, path):
        self.album_dir = Path(os.path.abspath(os.fspath(album_dir)))
        self.path = Path(os.path.abspath(os.fspath(path)))
        self._directories = []
        self._parts = ()
        self.descriptor = None
        try:
            relative = self.path.relative_to(self.album_dir)
            if (not relative.parts
                    or any(part in ("", ".", "..") for part in relative.parts)):
                raise OSError("repair source escaped the selected album")
            self._parts = relative.parts
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
            self._directories.append(os.open(self.album_dir, flags))
            for part in self._parts[:-1]:
                named = os.stat(
                    part,
                    dir_fd=self._directories[-1],
                    follow_symlinks=False,
                )
                child = os.open(part, flags, dir_fd=self._directories[-1])
                if (_stat_identity(named)[:3]
                        != _stat_identity(os.fstat(child))[:3]):
                    os.close(child)
                    raise OSError("repair source parent changed while opening")
                self._directories.append(child)

            file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW
            self.descriptor = os.open(
                self._parts[-1], file_flags, dir_fd=self._directories[-1])
            self._identity = _stat_identity(os.fstat(self.descriptor))
            if not stat.S_ISREG(self._identity[2]):
                raise OSError("repair source is not a regular file")
            if not self.intact():
                raise OSError("repair source changed before diagnosis")
        except BaseException:
            self.close()
            raise

    @property
    def size(self):
        return int(os.fstat(self.descriptor).st_size)

    @functools.cached_property
    def source_receipt(self):
        """The file's receipt, read in full only when it is offered a refill,
        or None when the file changed since it was opened."""
        receipt = self._seal_receipt()
        if receipt is None:
            return None
        sealed = receipt["file"]
        if (sealed["device"], sealed["inode"], sealed["type"], sealed["size"],
                sealed["mtime_ns"], sealed["ctime_ns"]) != self._identity:
            return None
        return receipt

    def _seal_receipt(self):
        before = os.fstat(self.descriptor)
        digest = hashlib.sha256()
        offset = 0
        while True:
            chunk = os.pread(self.descriptor, 1024 * 1024, offset)
            if not chunk:
                break
            digest.update(chunk)
            offset += len(chunk)
        after = os.fstat(self.descriptor)
        if _stat_identity(before) != _stat_identity(after):
            return None
        return {
            "relative": "/".join(self._parts),
            "file": {
                "type": stat.S_IFMT(after.st_mode),
                "device": int(after.st_dev),
                "inode": int(after.st_ino),
                "size": int(after.st_size),
                "mtime_ns": int(after.st_mtime_ns),
                "ctime_ns": int(after.st_ctime_ns),
                "sha256": digest.hexdigest(),
            },
        }

    def intact(self):
        if self.descriptor is None:
            return False
        try:
            if _stat_identity(os.fstat(self.descriptor)) != self._identity:
                return False
            root_named = os.stat(self.album_dir, follow_symlinks=False)
            if (_stat_identity(root_named)[:3]
                    != _stat_identity(os.fstat(self._directories[0]))[:3]):
                return False
            for index, part in enumerate(self._parts[:-1]):
                named = os.stat(
                    part,
                    dir_fd=self._directories[index],
                    follow_symlinks=False,
                )
                if (_stat_identity(named)[:3]
                        != _stat_identity(os.fstat(
                            self._directories[index + 1]))[:3]):
                    return False
            leaf = os.stat(
                self._parts[-1],
                dir_fd=self._directories[-1],
                follow_symlinks=False,
            )
            return _stat_identity(leaf) == _stat_identity(
                os.fstat(self.descriptor))
        except OSError:
            return False

    def close(self):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None
        while self._directories:
            os.close(self._directories.pop())

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()


def _fallback_held_meta(source):
    stem = source.path.stem
    match = re.match(r"^(\d+)[\s.\-]+(.+)$", stem)
    disc_match = re.match(
        r"(?:disc|cd)\s*0*(\d+)", source.path.parent.name, re.IGNORECASE)
    return {
        "title": match.group(2) if match else stem,
        "tracknumber": int(match.group(1)) if match else 0,
        "isrc": "",
        "discnumber": int(disc_match.group(1)) if disc_match else 1,
        "bits": 0,
        "sample_rate": 0,
        "channels": 0,
        "length": 0.0,
        "path": str(source.path),
        "size": source.size,
    }


def _read_held_audio_meta(source):
    """Read mutagen metadata only through the already-held source descriptor."""
    if not scanner.HAVE_MUTAGEN:
        return _fallback_held_meta(source)
    duplicate = os.dup(source.descriptor)
    try:
        with os.fdopen(duplicate, "rb") as fileobj:
            duplicate = None
            try:
                parsed = scanner.mutagen.File(fileobj, easy=True)
            except OSError:
                raise
            except Exception:
                return _fallback_held_meta(source)
    finally:
        if duplicate is not None:
            os.close(duplicate)
    if parsed is None:
        return _fallback_held_meta(source)
    tags = parsed.tags

    def first(key):
        value = tags.get(key) if tags else None
        return value[0] if value and isinstance(value, list) else ""

    title = first("title")
    if not title:
        return _fallback_held_meta(source)
    info = parsed.info
    return {
        "title": title,
        "album": first("album"),
        "artists": tags.get("artist") or tags.get("albumartist") or [],
        "isrc": first("isrc").strip().replace("-", "").upper(),
        "tracknumber": parse_track_num(first("tracknumber")),
        "discnumber": parse_track_num(first("discnumber")) or 1,
        "bits": getattr(info, "bits_per_sample", 0) if info else 0,
        "sample_rate": getattr(info, "sample_rate", 0) if info else 0,
        "channels": getattr(info, "channels", 0) if info else 0,
        "length": getattr(info, "length", 0.0) if info else 0.0,
        "path": str(source.path),
        "size": source.size,
    }


def _repair_flac_paths(album_dir):
    paths = []
    for path in iter_tree_no_symlinks(Path(album_dir)):
        if path.suffix.lower() == ".flac":
            paths.append(path)
    return sorted(paths)


def resealed_source_receipt(album_dir, receipt):
    """A fresh receipt for a file put back with the bytes, size and mtime
    ``receipt`` sealed, or None."""
    try:
        with _HeldRepairSource(
                album_dir, Path(album_dir) / receipt["relative"]) as source:
            fresh = source.source_receipt
    except OSError:
        return None
    if fresh is None or fresh["relative"] != receipt["relative"]:
        return None
    old, new = receipt["file"], fresh["file"]
    if any(old[key] != new[key] for key in ("type", "size", "mtime_ns", "sha256")):
        return None
    return fresh


def _append_verified_truncated(report, source, entry):
    """Attach the receipt only while the source is still the file every check
    read."""
    receipt = source.source_receipt
    if receipt is None or not source.intact():
        report["unverified"] += 1
        return
    entry["source_receipt"] = receipt
    report["verified_truncated"].append(entry)


def _append_verified_ok(report, source, isrc, *, record_isrc=True):
    if not source.intact():
        report["unverified"] += 1
        return
    report["verified_ok"] += 1
    if record_isrc:
        report["verified_ok_isrcs"][isrc] += 1


def _scan_held_repair_source(
        report, source, token, *, min_short_seconds, max_ratio,
        max_short_seconds, deep, only_isrcs, decode=True):
    """Diagnose one file while its descriptor, namespace and receipt hold.
    Without `decode` the file is taken to decode (the download already proved
    it) and only its length is compared."""
    et = _read_held_audio_meta(source)
    path = str(source.path)

    def decodes():
        return not decode or _flac_decode_ok(path, descriptor=source.descriptor)

    title = et.get("title") or source.path.stem
    isrc_raw = et.get("isrc") or ""
    isrc = isrc_raw.replace("-", "").upper().strip()
    try:
        flen = float(et.get("length") or 0)
    except (TypeError, ValueError):
        flen = 0.0

    if not isrc:
        entry = {"path": path, "title": title, "size_bytes": source.size}
        ok = flac_audio_ok(source.path, descriptor=source.descriptor) if decode else True
        if ok is None or not source.intact():
            report["unverified"] += 1
            return
        if not ok:
            entry["diagnostic"] = (
                "won't decode (frame-CRC or mid-file damage); "
                "re-download or replace from another source")
        report["no_isrc_tag"].append(entry)
        return

    sample_rate = int(et.get("sample_rate") or 0)
    bits = int(et.get("bits") or 0)
    channels = int(et.get("channels") or 2)
    actual_size = source.size
    audio_size = max(
        0,
        actual_size - flac_audio_offset(
            path, descriptor=source.descriptor),
    )
    looks_byte_short = (
        sample_rate > 0 and bits > 0 and flen > 0 and audio_size > 0
        and audio_size < flen * sample_rate * channels * (bits / 8)
        * _BYTE_SIZE_TRUNCATED_RATIO)
    if not deep and not looks_byte_short:
        dec = flac_audio_ok(Path(path), descriptor=source.descriptor)
        if dec is True:
            _append_verified_ok(report, source, isrc)
            return
        if dec is None:
            report["unverified"] += 1
            return

    if only_isrcs is not None and isrc not in only_isrcs:
        _append_verified_ok(report, source, isrc, record_isrc=False)
        return

    hits = _qobuz_tracks_by_isrc(isrc, token)
    if not source.intact():
        report["unverified"] += 1
        return
    if not hits:
        # These files have an ISRC, it just matched nothing at Qobuz. A broken
        # one belongs in the same bucket carrying a diagnostic, not under
        # "no ISRC tag", which is where the report files them by name.
        entry = {"path": path, "title": title, "isrc": isrc}
        if not decodes():
            entry["diagnostic"] = (
                "won't decode (frame-CRC or mid-file damage); "
                "re-download or replace from another source")
        if source.intact():
            report["isrc_no_match"].append(entry)
        else:
            report["unverified"] += 1
        return

    matched = _matching_repair_track(et, hits, source.album_dir)
    qt = matched or hits[0]
    try:
        qdur = float(qt.get("duration") or 0)
    except (TypeError, ValueError):
        qdur = 0.0
    entry = {
        "path": path,
        "file_length": flen,
        "qobuz_track": qt,
        "qobuz_duration": qdur,
        "isrc": isrc,
        "title": qt.get("title") or title,
        "track_number": qt.get("track_number") or et.get("tracknumber") or 0,
        # What the file on disk calls itself. The two above describe the
        # matched Qobuz recording, which is the right thing to re-download and
        # the wrong thing to name a review row after: when the ISRC is wrong
        # they belong to another song entirely, and the user is left looking
        # for a track their album does not contain.
        "local_title": title,
        "local_track_number": parse_track_num(et.get("tracknumber")),
    }
    same_recording = matched is not None

    def damaged(reason, **extra):
        """Offer a track refill only when its recording and edition match."""
        entry.update(reason=reason, **extra)
        if same_recording:
            _append_verified_truncated(report, source, entry)
            return
        entry["diagnostic"] = _MISMATCHED_DAMAGE_DIAGNOSTIC
        if source.intact():
            report["isrc_mismatch"].append(entry)
        else:
            report["unverified"] += 1

    if qdur <= 0:
        if not decodes():
            damaged("decode_failed")
        else:
            _append_verified_ok(report, source, isrc)
        return

    if sample_rate > 0 and bits > 0 and audio_size > 0:
        expected_uncompressed = qdur * sample_rate * channels * (bits / 8)
        if (audio_size < expected_uncompressed * _BYTE_SIZE_TRUNCATED_RATIO
                and shutil.which("flac") is not None
                and not decodes()):
            damaged("byte_size_short", actual_size=actual_size)
            return

    loss = qdur - flen
    short_against_match = (
        flen > 0 and loss > min_short_seconds
        and (flen < qdur * max_ratio or loss > max_short_seconds))
    if short_against_match and same_recording:
        _append_verified_truncated(report, source, entry)
        return

    # Short against a track that is not this song. The gap measures the
    # distance between two different recordings, so it proves nothing here, but
    # the file still gets its decode check below: real damage counts however
    # badly the tag is written.
    if not decodes():
        damaged("decode_failed", actual_size=actual_size)
        return

    if short_against_match:
        if source.intact():
            report["isrc_mismatch"].append(entry)
        else:
            report["unverified"] += 1
        return

    _append_verified_ok(report, source, isrc)


def scan_dir_for_isrc_repairs(album_dir, token,
                              *, min_short_seconds=30, max_ratio=0.85,
                              max_short_seconds=60, deep=True, only_isrcs=None,
                              decode=True):
    """Pair each FLAC in album_dir to its Qobuz recording via ISRC, then flag
    truncation by duration comparison (>30 s short AND either <85% ratio or
    >60 s short, so a long track can't hide a big shortfall behind the ratio).

    Buckets returned:

    * verified_truncated: ISRC match, same recording, and either short or
      undecodable. The only files ever deleted and refilled.
    * verified_ok: ISRC match, nothing wrong (a count, not a list).
    * no_isrc_tag: no ISRC tag, so recording identity is unverifiable.
    * isrc_no_match: ISRC tag present, Qobuz returned nothing.
    * isrc_mismatch: the recording or edition could not be confirmed. Two
      kinds live here. Without a `diagnostic` the file merely runs short, so
      the shortfall means nothing and it is reported and left alone. With a
      `diagnostic` the file is genuinely damaged, and it is offered a
      whole-album re-download, because the one thing that must never happen is
      refilling it from an ISRC that names another recording.

    Everything outside verified_truncated is surfaced without modification.
    Single-track refills require an ISRC match, a matching title, and the same
    mix, master and performance labels on the file and the Qobuz track.
    """
    report = {
        "verified_truncated": [],
        "verified_ok": 0,
        # Per-ISRC counts of files that matched a Qobuz recording AND passed
        # the gate, i.e. POSITIVELY re-verified, not merely "not flagged
        # truncated". A track whose lookup returned nothing lands in
        # isrc_no_match, so callers that must prove a refill is intact (repair
        # backup deletion) check membership here rather than absence from
        # verified_truncated.
        "verified_ok_isrcs": Counter(),
        "no_isrc_tag": [],
        "isrc_no_match": [],
        "isrc_mismatch": [],
        # FLACs we could not decode-check because the flac tool is absent,
        # counted so the summary can say so, never reported as "verified ok".
        "unverified": 0,
    }
    try:
        paths = _repair_flac_paths(album_dir)
    except OSError:
        report["unverified"] += 1
        return report

    for path in paths:
        try:
            with _HeldRepairSource(album_dir, path) as source:
                _scan_held_repair_source(
                    report, source, token,
                    min_short_seconds=min_short_seconds,
                    max_ratio=max_ratio,
                    max_short_seconds=max_short_seconds,
                    deep=deep,
                    only_isrcs=only_isrcs,
                    decode=decode,
                )
        except (AuthLost, QobuzUnavailable):
            raise
        except OSError:
            report["unverified"] += 1
    return report


_REPAIR_LOG_HEADER = (
    "# Replaced-tracks log: albums to refresh on offline clients\n"
    "#\n"
    "# Repair replaces a truncated file in place. Most music servers keep\n"
    "# the same track ID (so ratings/play counts survive), which means an\n"
    "# offline-sync client caching by ID will keep serving the old broken\n"
    "# file until you refresh that album. For each album below, on your\n"
    "# client: remove it from the offline cache, then re-download/re-sync.\n"
    "#\n"
    "# Once an entry is handled, delete its line. Append-only: anything\n"
    "# you leave behind is preserved across runs.\n"
    "#\n"
    "# Format:  YYYY-MM-DD HH:MM  |  Artist  |  Album  |  Track\n"
    "# " + ("─" * 70) + "\n"
    "\n"
)


def _one_log_line(value):
    """Collapse a tag value to a single safe field for the pipe-delimited log:
    '|' would break the column split, and an embedded newline (legal in Vorbis
    comments / ID3 and copied straight from tags) would split the row across two
    physical lines, which read_repair_log_entries then drops both halves of."""
    return ((value or "?").strip()
            .replace("|", "/").replace("\r", " ").replace("\n", " "))


def append_repair_log(entries):
    """Append `{artist, album, title}` rows to the replaced-tracks log
    so the user knows which albums to refresh on caching clients.

    Serializes through fcntl.flock so concurrent appenders can't interleave
    the header-check + header-write with each other's data lines. Today
    the run-lock serializes everything, but the locking here keeps the
    output parseable if a future code path ever writes outside that scope.
    """
    if not entries:
        return False
    ts = time.strftime("%Y-%m-%d %H:%M")
    payload_lines = []
    for e in entries:
        artist = _one_log_line(e.get("artist"))
        album  = _one_log_line(e.get("album"))
        title  = _one_log_line(e.get("title"))
        payload_lines.append(f"{ts}  |  {artist}  |  {album}  |  {title}\n")
    payload = "".join(payload_lines)
    try:
        cfg.REPAIR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with cfg.REPAIR_LOG_PATH.open("a+", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.seek(0, 2)
            content = (_REPAIR_LOG_HEADER + payload) if f.tell() == 0 else payload
            f.write(content)
        return True
    except OSError as e:
        log.info(fmt(C.YELLOW,
            f"  ⚠  Could not append to repair log ({cfg.REPAIR_LOG_PATH}): {e}"))
        return False


def read_repair_log_albums():
    """Parse the replaced-tracks log into one dict per album repair,
    newest-first, with that repair's track titles in the order it wrote them.

    Each data line is ``YYYY-MM-DD HH:MM  |  Artist  |  Album  |  Track``;
    header (``#`` comments) and blank lines are skipped, and lines that don't
    split into four pipe fields are dropped quietly rather than poisoning the
    view.
    """
    if not cfg.REPAIR_LOG_PATH.exists():
        return []
    repairs = {}
    try:
        with cfg.REPAIR_LOG_PATH.open("r", encoding="utf-8",
                                      errors="replace") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split("|")]
                if len(parts) != 4:
                    continue
                when, artist, album, title = parts
                repair = repairs.setdefault((when, artist, album), {
                    "when": when, "artist": artist, "album": album,
                    "tracks": []})
                repair["tracks"].append(title)
    except OSError:
        return []
    return list(reversed(repairs.values()))
