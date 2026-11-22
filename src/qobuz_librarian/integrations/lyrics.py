"""Lyric fetch integration - pre-import hook, post-import sidecars, state pruning."""

import os
import stat
from collections import Counter
from pathlib import Path, PurePosixPath

from qobuz_librarian import config as cfg
from qobuz_librarian.integrations import lyric_fetch, rip
from qobuz_librarian.safefile import identity_record
from qobuz_librarian.safefile import same_directory as _same_directory
from qobuz_librarian.ui_cli.colors import C, fmt
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import log, report_progress, vlog

# Lyric tag I/O (read/write embedded lyrics, materialise .lrc sidecars) needs
# only mutagen; fetching from providers additionally needs syncedlyrics
# (lyric_fetch.AVAILABLE). HAVE_LYRIC_FETCH gates the tag-I/O helpers so the
# sidecar pass still runs when only the provider library is missing; the fetch
# entry points below gate on lyric_fetch.AVAILABLE instead.
HAVE_LYRIC_FETCH = lyric_fetch.FLAC is not None


_LYRIC_RESOLVED_OUTCOMES = {
    "wrote-synced",
    "wrote-plain",
    "dry:wrote-synced",
    "dry:wrote-plain",
    "upgraded-synced",
    "dry:upgraded-synced",
    "copied-synced",
    "copied-plain",
    "dry:copied-synced",
    "dry:copied-plain",
    "already-synced",
    "already-plain",
    "kept-existing-plain",
    "kept-plain-lrc",
    "not-found",
    "skipped-instrumental",
    "skipped-long",
    "skipped-tags",
}
_LYRIC_QUEUED_OUTCOMES = {"providers-unavailable", "unsafe-path", "skipped-read-only"}


def _fetch_outcome_summary(counts, *, expected=None):
    """Split fetch tallies into resolved, queued, and failed outcomes."""
    values = {
        str(outcome): max(0, int(count))
        for outcome, count in (counts or {}).items()
        if outcome not in {"stopped", "stop-total", "stop-deferred"}
    }
    resolved = sum(values.get(outcome, 0) for outcome in _LYRIC_RESOLVED_OUTCOMES)
    queued = sum(values.get(outcome, 0) for outcome in _LYRIC_QUEUED_OUTCOMES)
    processed = sum(values.values())
    failed = max(0, processed - resolved - queued)
    if expected is not None:
        failed = max(failed, max(0, int(expected) - resolved - queued))
    return {
        "resolved": resolved,
        "queued": queued,
        "failed": failed,
    }


# ── Lyric hook ────────────────────────────────────────────────────────────────


def _run_lyric_hook(album_dir):
    """Embed lyrics into the staged FLACs under album_dir before import."""
    if not cfg.LYRICS_ENABLED:
        return
    if not lyric_fetch.AVAILABLE:
        log.info(fmt(C.GRAY, "     (lyric provider library not installed; skipping lyrics)"))
        return
    if album_dir is None or not album_dir.exists():
        return
    # When album_dir is STAGING_DIR (the single-album process path) the walk
    # would otherwise descend into albums parked under .beets_retry/ and
    # re-fetch their lyrics on every run; skip them, as beets' tag prep does.
    from qobuz_librarian.integrations.beets import _under_retry_dir

    try:
        flacs = sorted(p for p in album_dir.rglob("*.flac") if not _under_retry_dir(p))
    except OSError as e:
        log.info(fmt(C.YELLOW, f"  ⚠  Lyric hook: couldn't list {album_dir}: {e}."))
        return
    if not flacs:
        return
    log.info(fmt(C.CYAN, f"  ⟳  Checking lyrics for {plural(len(flacs), 'track')}…"))
    # lyric_fetch's state writer assumes its parent exists; ensure DATA_DIR is
    # created up-front so first-run (no other writer has touched DATA_DIR
    # yet) doesn't crash inside the lyric hook.
    try:
        cfg.LYRIC_FETCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        # Always embed here, regardless of cfg.LYRICS_FORMAT. This runs on the
        # *staging* dir before beets moves files: a .lrc written next to a
        # staging FLAC is orphaned the moment beets relocates+renames the
        # track (beets moves only the audio).
        # A provider asking to be asked later is not waited for here, where
        # the download waits too; the next Lyrics run looks those tracks up.
        skips = Counter()
        counts = lyric_fetch.fetch_for_paths(
            flacs,
            owned_root=cfg.STAGING_DIR,
            log=log,
            providers=cfg.LYRICS_PROVIDERS or None,
            lyrics_format="embed",
            state_path=cfg.LYRIC_FETCH_STATE_FILE,
            progress_cb=lambda c, t, _name, note: report_progress(
                " · ".join(filter(None, ("Fetching lyrics", note))), c, t,
                unit="track"),
            busy_patience=0,
            track_detail=True,
            skips=skips,
        )
    except Exception as e:
        log.info(fmt(C.YELLOW, f"  ⚠  Lyric hook failed: {e}."))
        return
    synced = counts.get("wrote-synced", 0) + counts.get("upgraded-synced", 0)
    plain = counts.get("wrote-plain", 0)
    nofnd = counts.get("not-found", 0)
    already = sum(
        counts.get(outcome, 0)
        for outcome in (
            "already-synced", "already-plain", "kept-existing-plain",
            "kept-plain-lrc",
            "copied-synced", "copied-plain",
        )
    ) + skips["lyrics"]
    unavailable = counts.get("providers-unavailable", 0)

    outcomes = _fetch_outcome_summary(
        counts, expected=len(flacs) - sum(skips.values()))
    failed = outcomes["failed"]
    told = [f"{count} {what}" for count, what in (
        (synced, "synced"), (plain, "plain"),
        (already, "already had them"), (nofnd, "not found")) if count]
    if 1 < already == len(flacs):
        told = [f"all {already} already had them"]
    parts = [", ".join(told)] if told else []
    instrumental = counts.get("skipped-instrumental", 0)
    if instrumental:
        parts.append(f"{instrumental} skipped as instrumental")
    too_long = counts.get("skipped-long", 0)
    if too_long:
        parts.append(f"{too_long} skipped (too long)")
    no_tags = counts.get("skipped-tags", 0)
    if no_tags:
        parts.append(f"{no_tags} skipped (missing tags)")
    if unavailable:
        parts.append(f"{unavailable} not answered by a provider, left to the "
                     "next Lyrics run")
    read_only = counts.get("skipped-read-only", 0)
    if read_only:
        parts.append(f"{read_only} read-only, left alone")
    if skips["checked"]:
        parts.append(f"{skips['checked']} checked before")
    if failed:
        parts.append(f"{failed} failed")
    if not parts:
        return
    msg = "lyrics: " + "; ".join(parts)
    if failed or unavailable or read_only:
        log.info(fmt(C.YELLOW, f"  ⚠  {msg}."))
    else:
        log.info(fmt(C.GREEN, f"  ✓  {msg}."))


def _regular_file_identity(value):
    return identity_record(value) if stat.S_ISREG(value.st_mode) else None


def _directory_identity(value):
    return identity_record(value) if stat.S_ISDIR(value.st_mode) else None


def _valid_file_identity(value):
    return isinstance(value, dict) and all(
        type(value.get(field)) is int
        for field in ("device", "inode", "size", "modified_ns", "changed_ns")
    )


def _open_nofollow_directory(path, *, dir_fd=None):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    return os.open(os.fspath(path), flags, dir_fd=dir_fd)


def _open_library_chain(path):
    """Hold one real directory reached beneath MUSIC_ROOT without symlinks."""
    try:
        root = Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
        candidate = Path(os.path.abspath(os.fspath(path)))
        if os.path.commonpath((os.fspath(root), os.fspath(candidate))) != os.fspath(root):
            return None
        relative = os.path.relpath(candidate, root)
        parts = [] if relative == "." else list(Path(relative).parts)
        if any(part in ("", ".", "..") for part in parts):
            return None
    except (OSError, TypeError, ValueError):
        return None

    descriptors = []
    try:
        root_fd = _open_nofollow_directory(root)
        descriptors.append(root_fd)
        if not _same_directory(os.fstat(root_fd), os.stat(root, follow_symlinks=False)):
            raise OSError("music root changed while it was opened")
        for part in parts:
            child = _open_nofollow_directory(part, dir_fd=descriptors[-1])
            try:
                if not _same_directory(
                    os.fstat(child),
                    os.stat(
                        part,
                        dir_fd=descriptors[-1],
                        follow_symlinks=False,
                    ),
                ):
                    raise OSError("library directory changed while it was opened")
            except Exception:
                os.close(child)
                raise
            descriptors.append(child)
        return root, parts, descriptors
    except (OSError, TypeError, ValueError):
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        return None


def _library_chain_is_named(root, parts, descriptors):
    if len(descriptors) != len(parts) + 1:
        return False
    try:
        if not _same_directory(
            os.fstat(descriptors[0]),
            os.stat(root, follow_symlinks=False),
        ):
            return False
        for index, part in enumerate(parts):
            if not _same_directory(
                os.fstat(descriptors[index + 1]),
                os.stat(
                    part,
                    dir_fd=descriptors[index],
                    follow_symlinks=False,
                ),
            ):
                return False
    except (OSError, TypeError, ValueError):
        return False
    return True


def _iter_library_flacs(public_dir, root, parts, descriptors):
    """Yield regular FLAC leaves from held, no-follow directory descriptors."""
    if not _library_chain_is_named(root, parts, descriptors):
        return
    try:
        snapshot = []
        with os.scandir(descriptors[-1]) as entries:
            for entry in entries:
                try:
                    snapshot.append(
                        (
                            entry.name,
                            entry.stat(follow_symlinks=False),
                        )
                    )
                except OSError:
                    continue
        snapshot.sort(key=lambda value: value[0].casefold())
    except OSError:
        return

    for entry_name, entry_stat in snapshot:
        if stat.S_ISREG(entry_stat.st_mode):
            if not entry_name.lower().endswith(".flac"):
                continue
            parent_fd = os.dup(descriptors[-1])
            frozen_parts = tuple(parts)
            frozen_descriptors = tuple(descriptors)

            def parent_guard(root=root, parts=frozen_parts, descriptors=frozen_descriptors):
                return _library_chain_is_named(root, parts, descriptors)

            try:
                yield public_dir / entry_name, parent_fd, parent_guard
            finally:
                os.close(parent_fd)
            continue
        if not stat.S_ISDIR(entry_stat.st_mode):
            continue
        child = None
        entered = False
        try:
            child = _open_nofollow_directory(entry_name, dir_fd=descriptors[-1])
            named = os.stat(
                entry_name,
                dir_fd=descriptors[-1],
                follow_symlinks=False,
            )
            if not _same_directory(os.fstat(child), named):
                continue
            descriptors.append(child)
            parts.append(entry_name)
            entered = True
            child = None
            yield from _iter_library_flacs(
                public_dir / entry_name,
                root,
                parts,
                descriptors,
            )
        except OSError:
            continue
        finally:
            if child is not None:
                os.close(child)
            if entered:
                parts.pop()
                os.close(descriptors.pop())


def _iter_secure_album_flacs(path, seen):
    opened = _open_library_chain(path)
    if opened is None:
        return
    root, parts, descriptors = opened
    try:
        held = os.fstat(descriptors[-1])
        key = (int(held.st_dev), int(held.st_ino))
        if key in seen:
            return
        seen.add(key)
        yield from _iter_library_flacs(
            Path(os.path.abspath(os.fspath(path))),
            root,
            parts,
            descriptors,
        )
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _owned_track_target(payload):
    """Return one strictly shaped sealed ownership target, or ``None``."""
    try:
        root = Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
        if (
            not isinstance(payload, dict)
            or type(payload.get("version")) is not int
            or payload.get("version") != 1
            or payload.get("sealed") is not True
            or payload.get("root") != os.fspath(root)
            or not _valid_file_identity(payload.get("root_identity"))
        ):
            return None
        records = payload.get("items")
        if not isinstance(records, list) or len(records) != 1:
            return None
        record = records[0]
        if (
            not isinstance(record, dict)
            or not _valid_file_identity(record.get("file"))
            or not isinstance(record.get("relative"), str)
        ):
            return None
        relative = PurePosixPath(record["relative"])
        if (
            relative.is_absolute()
            or len(relative.parts) < 2
            or any(part in ("", ".", "..") for part in relative.parts)
        ):
            return None
        return root.joinpath(*relative.parts), record
    except (OSError, TypeError, ValueError):
        return None


def _owned_scope_identities(record, relative):
    scope = record.get("album_scope") if isinstance(record, dict) else None
    if (
        not isinstance(scope, dict)
        or set(scope) != {"relative", "directory", "ancestors"}
        or not isinstance(scope.get("relative"), str)
        or not _valid_file_identity(scope.get("directory"))
        or not isinstance(scope.get("ancestors"), list)
    ):
        return None
    scope_relative = PurePosixPath(scope["relative"])
    if (
        scope_relative.is_absolute()
        or not scope_relative.parts
        or any(part in ("", ".", "..") for part in scope_relative.parts)
        or len(scope_relative.parts) >= len(relative.parts)
        or relative.parts[: len(scope_relative.parts)] != scope_relative.parts
        or len(scope["ancestors"]) != len(scope_relative.parts) - 1
    ):
        return None
    identities = []
    for length, ancestor in enumerate(scope["ancestors"], start=1):
        expected_relative = PurePosixPath(*scope_relative.parts[:length]).as_posix()
        if (
            not isinstance(ancestor, dict)
            or set(ancestor) != {"relative", "directory"}
            or ancestor.get("relative") != expected_relative
            or not _valid_file_identity(ancestor.get("directory"))
        ):
            return None
        identities.append(ancestor["directory"])
    identities.append(scope["directory"])
    return identities


def _iter_secure_owned_flacs(payloads):
    """Yield only exact sealed ownership leaves, holding their full chains."""
    yielded = set()
    for payload in payloads or ():
        target = _owned_track_target(payload)
        if target is None:
            continue
        path, record = target
        if path in yielded:
            continue
        yielded.add(path)
        relative = PurePosixPath(record["relative"])
        scope_identities = _owned_scope_identities(record, relative)
        if scope_identities is None:
            continue
        opened = _open_library_chain(path.parent)
        if opened is None:
            continue
        root, parts, descriptors = opened
        try:
            if _directory_identity(os.fstat(descriptors[0])) != payload["root_identity"] or len(
                descriptors
            ) != len(relative.parts):
                continue
            scope_valid = True
            for index, expected in enumerate(scope_identities, start=1):
                if (
                    index >= len(descriptors)
                    or _directory_identity(os.fstat(descriptors[index])) != expected
                ):
                    scope_valid = False
                    break
            if not scope_valid or not _library_chain_is_named(root, parts, descriptors):
                continue
            parent_fd = os.dup(descriptors[-1])
            frozen_parts = tuple(parts)
            frozen_descriptors = tuple(descriptors)

            def parent_guard(root=root, parts=frozen_parts, descriptors=frozen_descriptors):
                return _library_chain_is_named(root, parts, descriptors)

            try:
                yield path, parent_fd, parent_guard, record["file"]
            finally:
                os.close(parent_fd)
        except OSError:
            continue
        finally:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _iter_post_import_flacs(album_dirs, owned_tracks, seen):
    targets = [
        target
        for payload in owned_tracks or ()
        if (target := _owned_track_target(payload)) is not None
    ]
    excluded = {path for path, _record in targets}
    root = Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
    owned_albums = {
        root.joinpath(*PurePosixPath(scope["relative"]).parts)
        for _path, record in targets
        if isinstance(scope := record.get("album_scope"), dict)
        and isinstance(scope.get("relative"), str)
    }
    yield from _iter_secure_owned_flacs(owned_tracks)
    for directory in album_dirs:
        if directory is None or not isinstance(directory, Path):
            continue
        if Path(os.path.abspath(os.fspath(directory))) in owned_albums:
            continue
        for path, parent_fd, parent_guard in _iter_secure_album_flacs(directory, seen):
            if path in excluded:
                continue
            yield path, parent_fd, parent_guard, None


def _held_track_is_named(path, parent_fd, track_fd, expected, parent_guard=None):
    try:
        held_parent = os.fstat(parent_fd)
        held_track = _regular_file_identity(os.fstat(track_fd))
        named_track = _regular_file_identity(
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        )
        if parent_guard is None:
            named_parent = os.stat(path.parent, follow_symlinks=False)
            parent_matches = (
                stat.S_ISDIR(named_parent.st_mode)
                and held_parent.st_dev == named_parent.st_dev
                and held_parent.st_ino == named_parent.st_ino
            )
        else:
            parent_matches = parent_guard() is True
    except OSError:
        return False
    return (
        stat.S_ISDIR(held_parent.st_mode)
        and parent_matches
        and held_track == expected
        and named_track == expected
    )


def _created_sidecar_receipt(path, parent_fd, track_identity, creation):
    if not isinstance(creation, dict):
        return None
    file_identity = creation.get("file")
    sidecar = path.with_suffix(".lrc")
    if creation.get("path") != os.path.abspath(os.fspath(sidecar)) or not _valid_file_identity(
        file_identity
    ):
        return None
    try:
        named = _regular_file_identity(
            os.stat(sidecar.name, dir_fd=parent_fd, follow_symlinks=False)
        )
    except OSError:
        return None
    if named != file_identity:
        return None
    return {
        "path": creation["path"],
        "track_path": os.path.abspath(os.fspath(path)),
        "track": dict(track_identity),
        "file": dict(file_identity),
    }


def _sidecar_identity_from_receipt(path, receipt):
    if not isinstance(receipt, dict):
        return None
    sidecar = path.with_suffix(".lrc")
    identity = receipt.get("file")
    if (
        receipt.get("path") != os.path.abspath(os.fspath(sidecar))
        or not _valid_file_identity(identity)
    ):
        return None
    return dict(identity)


def write_post_import_sidecars(
    album_dirs,
    *,
    identity_changes_out=None,
    created_files_out=None,
    directory_changes_out=None,
    owned_tracks=None,
    track_signatures=None,
):
    """Write .lrc sidecars next to imported FLACs when LYRICS_FORMAT
    requests them. Embedded tag is removed afterward for 'sidecar' mode,
    kept for 'both'. An existing .lrc is replaced only when it is plain and
    the tag is synced. Optional output lists receive exact identity changes
    and sidecars that this pass can prove it created, plus exact parent-directory
    identity transitions caused by those writes. ``owned_tracks`` contains
    sealed one-file import receipts; those leaves are handled first, while
    their original identities still match, and their albums' other tracks
    are left alone. With ``track_signatures`` only the tracks those name
    are handled in ``album_dirs``."""
    return _SidecarPass(
        album_dirs,
        identity_changes_out=identity_changes_out,
        created_files_out=created_files_out,
        directory_changes_out=directory_changes_out,
        owned_tracks=owned_tracks,
        track_signatures=track_signatures,
    ).run()


_NEXT_TRACK = object()


class _SidecarPass:
    """One post-import sidecar pass, track by track."""

    def __init__(
        self,
        album_dirs,
        *,
        identity_changes_out,
        created_files_out,
        directory_changes_out,
        owned_tracks,
        track_signatures,
    ):
        self.album_dirs = album_dirs
        self.identity_changes_out = identity_changes_out
        self.created_files_out = created_files_out
        self.directory_changes_out = directory_changes_out
        self.owned_tracks = owned_tracks
        self.track_signatures = track_signatures

    def run(self):
        if not cfg.LYRICS_ENABLED or not HAVE_LYRIC_FETCH:
            return
        # Only mutagen is needed to read the embedded tag and write the .lrc -
        # NOT syncedlyrics. (lyric_fetch.AVAILABLE conflates the two; gating on
        # it here would wrongly skip sidecars whenever the provider lib is
        # absent but mutagen is present.)
        if getattr(lyric_fetch, "FLAC", None) is None:
            return
        lyr_fmt = (cfg.LYRICS_FORMAT or "embed").strip().lower()
        if lyr_fmt not in ("sidecar", "both"):
            return
        self.strip_tag = lyr_fmt == "sidecar"
        self.wanted = {tuple(sig) for sig in self.track_signatures or () if sig} or None
        self.seen = set()
        self.written = 0
        self.kept = 0
        self.stripped = 0
        self.failed = 0
        self.strip_failed = 0
        self.strip_read_only = 0
        self.strip_skipped = 0
        for track in _iter_post_import_flacs(
            self.album_dirs, self.owned_tracks, self.seen
        ):
            self.fp, self.parent_fd, self.parent_guard, self.expected_track = track
            self._handle_track()
        self._report()

    def _handle_track(self):
        if self._open_track() is _NEXT_TRACK:
            return
        try:
            if self._write_sidecar() is _NEXT_TRACK:
                return
            if self.strip_tag:
                self._strip_tag()
        finally:
            if (
                isinstance(self.directory_changes_out, list)
                and self.directory_mutated
                and self.parent_identity is not None
            ):
                try:
                    after_parent = _directory_identity(os.fstat(self.parent_fd))
                    if (
                        after_parent is not None
                        and self.parent_guard() is True
                        and after_parent != self.parent_identity
                    ):
                        self.directory_changes_out.append(
                            {
                                "path": os.path.abspath(os.fspath(self.fp.parent)),
                                "before": dict(self.parent_identity),
                                "after": dict(after_parent),
                            }
                        )
                except OSError:
                    pass
            os.close(self.track_fd)

    def _open_track(self):
        self.track_fd = None
        self.parent_identity = None
        self.directory_mutated = False
        try:
            self.parent_identity = _directory_identity(os.fstat(self.parent_fd))
            if self.parent_identity is None:
                raise OSError("track parent is not a directory")
            track_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            self.track_fd = os.open(self.fp.name, track_flags, dir_fd=self.parent_fd)
            self.track_identity = _regular_file_identity(os.fstat(self.track_fd))
            if (
                self.track_identity is None
                or (self.expected_track is not None and self.track_identity != self.expected_track)
                or not _held_track_is_named(
                    self.fp, self.parent_fd, self.track_fd, self.track_identity, self.parent_guard
                )
            ):
                raise OSError("track identity changed")
            self.f = lyric_fetch.FLAC(f"/proc/self/fd/{self.track_fd}")
        except Exception as e:
            vlog(f"sidecar: FLAC open failed {self.fp}: {e}")
            self.failed += 1
            if self.track_fd is not None:
                os.close(self.track_fd)
            return _NEXT_TRACK

    def _track_guard(self):
        return _held_track_is_named(
            self.fp,
            self.parent_fd,
            self.track_fd,
            self.track_identity,
            self.parent_guard,
        )

    def _write_sidecar(self):
        if (self.wanted is not None and self.expected_track is None
                and rip._flac_signature(f"/proc/self/fd/{self.track_fd}") not in self.wanted):
            return _NEXT_TRACK
        content = lyric_fetch.get_existing_lyrics(self.f)
        if not content or not content.strip():
            return _NEXT_TRACK
        if not _held_track_is_named(
            self.fp, self.parent_fd, self.track_fd, self.track_identity, self.parent_guard
        ):
            vlog(f"sidecar: track changed before write {self.fp}")
            self.failed += 1
            return _NEXT_TRACK
        creation = {}
        self.sidecar_identity_receipt = {}
        sidecar_directory_mutation = {}
        sidecar_error = None
        try:
            if isinstance(self.created_files_out, list):
                self.sidecar_written = lyric_fetch.write_sidecar(
                    self.fp,
                    content,
                    creation_out=creation,
                    directory_mutation_out=sidecar_directory_mutation,
                    parent_fd=self.parent_fd,
                    parent_guard=self.parent_guard,
                    track_guard=self._track_guard,
                    sidecar_identity_out=self.sidecar_identity_receipt,
                )
            else:
                self.sidecar_written = lyric_fetch.write_sidecar(
                    self.fp,
                    content,
                    parent_fd=self.parent_fd,
                    parent_guard=self.parent_guard,
                    track_guard=self._track_guard,
                    sidecar_identity_out=self.sidecar_identity_receipt,
                )
        except OSError as e:
            sidecar_error = e
        finally:
            track_unchanged = _held_track_is_named(
                self.fp, self.parent_fd, self.track_fd, self.track_identity, self.parent_guard
            )
            created = _created_sidecar_receipt(
                self.fp, self.parent_fd, self.track_identity, creation
            )
            if isinstance(self.created_files_out, list) and track_unchanged and created is not None:
                self.created_files_out.append(created)
                # Publication completed even if the following parent
                # fsync reported an error. Carry the exact directory
                # identity forward so Undo remains bound to the file
                # that actually landed.
                self.directory_mutated = True
            if sidecar_directory_mutation.get("mutated") is True:
                self.directory_mutated = True
        if sidecar_error is not None:
            vlog(f"sidecar: write failed {self.fp}: {sidecar_error}")
            self.failed += 1
            return _NEXT_TRACK
        if not track_unchanged:
            vlog(f"sidecar: track changed during write {self.fp}")
            self.failed += 1
            return _NEXT_TRACK
        if self.sidecar_written:
            self.written += 1
            self.directory_mutated = True
        else:
            self.kept += 1

    def _strip_tag(self):
        change = {}
        tag_directory_mutation = {}
        self.sidecar_fd = None
        try:
            self._open_sidecar()

            def sidecar_guard(
                sidecar=self.sidecar,
                sidecar_fd=self.sidecar_fd,
                sidecar_identity=self.sidecar_identity,
                parent_fd=self.parent_fd,
                parent_guard=self.parent_guard,
            ):
                return _held_track_is_named(
                    sidecar,
                    parent_fd,
                    sidecar_fd,
                    sidecar_identity,
                    parent_guard,
                )

            preserved = lyric_fetch._read_bound_sidecar(
                self.fp, self.parent_fd, self.parent_guard, self._track_guard)
            if preserved is None or any(
                    value.strip() != preserved.strip()
                    for value in lyric_fetch.embedded_lyric_values(self.f)):
                self.strip_skipped += 1
                return
            had = False
            for _k in ("lyrics", "unsyncedlyrics"):
                if _k in self.f.tags:
                    del self.f.tags[_k]
                    had = True
            if had:
                if not _held_track_is_named(
                    self.fp, self.parent_fd, self.track_fd, self.track_identity, self.parent_guard
                ):
                    raise OSError("track changed before tag rewrite")
                if isinstance(self.identity_changes_out, list):
                    lyric_fetch.save_flac_tags(
                        self.f,
                        self.fp,
                        identity_change_out=change,
                        directory_mutation_out=(tag_directory_mutation),
                        parent_fd=self.parent_fd,
                        parent_guard=self.parent_guard,
                        expected_identity=self.track_identity,
                        commit_guard=sidecar_guard,
                    )
                else:
                    lyric_fetch.save_flac_tags(
                        self.f,
                        self.fp,
                        parent_fd=self.parent_fd,
                        parent_guard=self.parent_guard,
                        expected_identity=self.track_identity,
                        commit_guard=sidecar_guard,
                    )
                self.stripped += 1
        except lyric_fetch.ReadOnlyTrack:
            self.strip_read_only += 1
        except Exception as e:
            self.strip_failed += 1
            action = "wrote" if self.sidecar_written else "kept"
            log.info(
                fmt(
                    C.YELLOW,
                    f"  ⚠  lyrics: {action} "
                    f"{self.fp.with_suffix('.lrc').name}, but couldn't "
                    "remove embedded lyrics from "
                    f"{self.fp.name}: {e}.",
                )
            )
        finally:
            if self.sidecar_fd is not None:
                os.close(self.sidecar_fd)
            if (
                isinstance(self.identity_changes_out, list)
                and change.get("path") == os.path.abspath(os.fspath(self.fp))
                and _valid_file_identity(change.get("before"))
                and _valid_file_identity(change.get("after"))
                and change["before"] != change["after"]
            ):
                self.identity_changes_out.append(dict(change))
                self.directory_mutated = True
            if tag_directory_mutation.get("mutated") is True:
                self.directory_mutated = True

    def _open_sidecar(self):
        self.sidecar = self.fp.with_suffix(".lrc")
        self.sidecar_identity = _sidecar_identity_from_receipt(
            self.fp, self.sidecar_identity_receipt
        )
        if self.sidecar_identity is None:
            raise OSError(
                f"{self.sidecar.name}: final sidecar identity wasn't proved"
            )
        sidecar_flags = (
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        self.sidecar_fd = os.open(self.sidecar.name, sidecar_flags, dir_fd=self.parent_fd)
        if not _held_track_is_named(
            self.sidecar,
            self.parent_fd,
            self.sidecar_fd,
            self.sidecar_identity,
            self.parent_guard,
        ):
            raise OSError(f"{self.sidecar.name}: sidecar changed before tag strip")

    def _report(self):
        ready = self.written + self.kept
        if ready or self.failed:
            actions = []
            if self.written:
                actions.append(f"wrote {plural(self.written, '.lrc sidecar')}")
            if self.kept:
                actions.append(f"kept {plural(self.kept, 'existing .lrc sidecar')}")
            if self.strip_tag and ready:
                actions[-1] += (
                    " (embedded tags removed)"
                    if self.stripped == ready
                    else f" (embedded tag removal confirmed for {self.stripped} of {ready})"
                )
            if self.failed:
                noun = "sidecar" if self.failed == 1 else "sidecars"
                actions.append(f"{self.failed} {noun} failed")
            if self.strip_tag and ready:
                if self.strip_failed:
                    noun = "removal" if self.strip_failed == 1 else "removals"
                    actions.append(f"{self.strip_failed} tag {noun} failed")
                if self.strip_read_only:
                    actions.append(f"{plural(self.strip_read_only, 'track')} read-only, "
                                   "so the lyrics stay in the tag too")
                if self.strip_skipped:
                    actions.append(f"{plural(self.strip_skipped, 'track')} with a "
                                   "different .lrc, so the lyrics stay in the tag too")
            warning = bool(self.failed or self.strip_failed)
            marker = "⚠" if warning else "✓"
            colour = C.YELLOW if warning else C.GREEN
            log.info(fmt(colour, f"  {marker}  lyrics: {'; '.join(actions)}."))


def _prune_lyric_state_orphans():
    """Drop staging-path keys from lyric_fetch's state file -
    they're orphaned the moment beets moves the file out of staging.

    Done as one atomic read-modify-write under the state lock: a plain
    load→modify→save here can clobber an entry a concurrent import-hook
    checkpoint just added (a CLI run and the web worker write the same file)."""
    if not HAVE_LYRIC_FETCH:
        return
    staging_prefix = str(cfg.STAGING_DIR)
    if not staging_prefix.endswith("/"):
        staging_prefix += "/"
    pruned = 0

    def _drop_staging(state):
        nonlocal pruned
        for k in [k for k in state if k.startswith(staging_prefix)]:
            del state[k]
            pruned += 1

    try:
        lyric_fetch.update_state(_drop_staging, cfg.LYRIC_FETCH_STATE_FILE)
    except Exception as e:
        vlog(f"_prune_lyric_state_orphans: update failed: {e}")
        return
    if pruned:
        vlog(f"pruned {pruned} orphan lyric-state entry(ies) (staging-keyed)")
