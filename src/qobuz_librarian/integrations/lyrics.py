"""Lyric fetch integration - pre-import hook, retry manifest, state pruning."""

import os
import stat
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file
from qobuz_librarian.file_exclusion import acquire_inode_write_exclusion
from qobuz_librarian.integrations import lyric_fetch
from qobuz_librarian.ui_cli.colors import C, fmt
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
    "already-synced",
    "already-plain",
    "kept-existing-plain",
    "not-found",
}
_LYRIC_QUEUED_OUTCOMES = {"providers-unavailable", "unsafe-path"}


def _fetch_outcome_summary(counts, *, expected=None):
    """Split fetch tallies into resolved, queued, and failed outcomes."""
    values = {
        str(outcome): max(0, int(count))
        for outcome, count in (counts or {}).items()
        if outcome not in {"stopped", "stop-total"}
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


def summarize_lyric_retry(counts, *, attempted, remaining):
    """Reconcile retry outcomes without treating dropped errors as success."""
    outcomes = _fetch_outcome_summary(counts)
    attempted = max(0, int(attempted))
    remaining = min(attempted, max(0, int(remaining)))
    removed = attempted - remaining
    resolved = min(removed, outcomes["resolved"])
    failed = max(outcomes["failed"], removed - resolved)
    return {
        "resolved": resolved,
        "failed": failed,
        "remaining": remaining,
    }


# ── Lyric hook ────────────────────────────────────────────────────────────────


def _run_lyric_hook(album_dir):
    """Pre-import lyric fetch. Returns (counts, transient_signatures);
    the caller resolves signatures to post-import paths and persists
    them via _record_post_import_lyric_retry."""
    empty_result = ({}, [])
    if not cfg.LYRICS_ENABLED:
        return empty_result
    if not lyric_fetch.AVAILABLE:
        log.info(fmt(C.GRAY, "     (lyric provider library not installed; skipping lyrics)"))
        return empty_result
    if album_dir is None or not album_dir.exists():
        return empty_result
    # When album_dir is STAGING_DIR (the single-album process path) the walk
    # would otherwise descend into albums parked under .beets_retry/ and
    # re-fetch their lyrics on every run; skip them, as beets' tag prep does.
    from qobuz_librarian.integrations.beets import _under_retry_dir

    try:
        flacs = sorted(p for p in album_dir.rglob("*.flac") if not _under_retry_dir(p))
    except OSError as e:
        log.info(fmt(C.YELLOW, f"  ⚠  Lyric hook: couldn't list {album_dir}: {e}."))
        return empty_result
    if not flacs:
        return empty_result
    log.info(fmt(C.CYAN, f"  ⟳  Checking lyrics for {len(flacs)} track(s)…"))
    # lyric_fetch.save_state assumes its parent exists; ensure DATA_DIR is
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
        counts = lyric_fetch.fetch_for_paths(
            flacs,
            owned_root=cfg.STAGING_DIR,
            log=log,
            providers=cfg.LYRICS_PROVIDERS or None,
            lyrics_format="embed",
            state_path=cfg.LYRIC_FETCH_STATE_FILE,
            progress_cb=lambda c, t, name: report_progress("Fetching lyrics", c, t, name),
        )
    except Exception as e:
        log.info(fmt(C.YELLOW, f"  ⚠  Lyric hook failed: {e}."))
        return empty_result
    synced = counts.get("wrote-synced", 0)
    plain = counts.get("wrote-plain", 0)
    nofnd = counts.get("not-found", 0)
    already = sum(
        counts.get(outcome, 0)
        for outcome in (
            "already-synced", "already-plain", "kept-existing-plain"
        )
    )
    # Files where every provider was unavailable when their turn came up. The
    # fetcher tallies these under "providers-unavailable" and marks the file
    # status="transient" in its state file; we surface the count and queue them
    # for retry on next launch.
    unavailable = counts.get("providers-unavailable", 0)

    outcomes = _fetch_outcome_summary(counts, expected=len(flacs))
    failed = outcomes["failed"]
    msg = (
        f"lyrics: {synced} synced, {plain} plain, "
        f"{already} already, {nofnd} not found"
    )
    if unavailable:
        msg += f"; {unavailable} provider-unavailable and need retry after import"
    if failed:
        msg += f"; {failed} failed"
    if failed or unavailable:
        log.info(fmt(C.YELLOW, f"  ⚠  {msg}."))
    else:
        log.info(fmt(C.GREEN, f"  ✓  {msg}."))

    # Capture tag signatures for transient files. The state lookup uses
    # staging paths (matches what lyric_fetch just wrote); the caller
    # will use these signatures to find the post-import paths after
    # beets imports.
    transient_signatures = []
    if unavailable:
        try:
            state = lyric_fetch.load_state(cfg.LYRIC_FETCH_STATE_FILE)
        except Exception as e:
            vlog(f"_run_lyric_hook: state load failed: {e}")
            state = {}
        from qobuz_librarian.integrations.rip import _flac_signature

        for fp in flacs:
            st = state.get(str(fp))
            if st is None or st.status != "transient":
                continue
            sig = _flac_signature(fp)
            if sig is not None:
                transient_signatures.append((sig, str(fp)))
    return counts, transient_signatures


def _resolve_signatures_to_paths(signatures, search_dirs):
    """Walk search_dirs and match each FLAC's tag signature. Returns the list of
    post-import paths that match.
    """
    if not signatures:
        return []
    signatures_needed = Counter(sig for sig, _ in signatures)
    found = []
    seen = set()
    from qobuz_librarian.integrations.rip import _flac_signature

    for d in search_dirs:
        if not signatures_needed:
            break
        if d is None or not isinstance(d, Path):
            continue
        try:
            key = str(d.resolve())
        except OSError:
            key = str(d)
        if key in seen:
            continue
        seen.add(key)
        if not d.exists():
            continue
        try:
            for fp in d.rglob("*.flac"):
                if not fp.is_file():
                    continue
                sig = _flac_signature(fp)
                if sig is not None and signatures_needed.get(sig, 0) > 0:
                    found.append(str(fp))
                    signatures_needed[sig] -= 1
                    if signatures_needed[sig] <= 0:
                        del signatures_needed[sig]
                    if not signatures_needed:
                        break
        except OSError as e:
            vlog(f"_resolve_signatures: rglob failed in {d}: {e}")
    return found


def _regular_file_identity(value):
    if not stat.S_ISREG(value.st_mode):
        return None
    return {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "size": int(value.st_size),
        "modified_ns": int(value.st_mtime_ns),
        "changed_ns": int(value.st_ctime_ns),
    }


def _directory_identity(value):
    if not stat.S_ISDIR(value.st_mode):
        return None
    return {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "size": int(value.st_size),
        "modified_ns": int(value.st_mtime_ns),
        "changed_ns": int(value.st_ctime_ns),
    }


def _valid_file_identity(value):
    return isinstance(value, dict) and all(
        type(value.get(field)) is int
        for field in ("device", "inode", "size", "modified_ns", "changed_ns")
    )


def _open_nofollow_directory(path, *, dir_fd=None):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError("safe no-follow file operations are unavailable")
    flags = os.O_RDONLY | directory | nofollow
    flags |= getattr(os, "O_CLOEXEC", 0)
    return os.open(os.fspath(path), flags, dir_fd=dir_fd)


def _same_directory(left, right):
    return (
        stat.S_ISDIR(left.st_mode)
        and stat.S_ISDIR(right.st_mode)
        and int(left.st_dev) == int(right.st_dev)
        and int(left.st_ino) == int(right.st_ino)
    )


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
    excluded = {
        target[0]
        for payload in owned_tracks or ()
        if (target := _owned_track_target(payload)) is not None
    }
    for directory in album_dirs:
        if directory is None or not isinstance(directory, Path):
            continue
        for path, parent_fd, parent_guard in _iter_secure_album_flacs(directory, seen):
            if path in excluded:
                continue
            yield path, parent_fd, parent_guard, None
    yield from _iter_secure_owned_flacs(owned_tracks)


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
):
    """Write .lrc sidecars next to imported FLACs when LYRICS_FORMAT
    requests them. Embedded tag is removed afterward for 'sidecar' mode,
    kept for 'both'. Optional output lists receive exact identity changes and
    sidecars that this pass can prove it created, plus exact parent-directory
    identity transitions caused by those writes. ``owned_tracks`` contains
    sealed one-file import receipts; those leaves are excluded from album
    scans and handled only while their original identities still match."""
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
    strip_tag = lyr_fmt == "sidecar"
    seen = set()
    written = 0
    kept = 0
    stripped = 0
    failed = 0
    strip_failed = 0
    for fp, parent_fd, parent_guard, expected_track in _iter_post_import_flacs(
        album_dirs, owned_tracks, seen
    ):
        track_fd = None
        parent_identity = None
        directory_mutated = False
        nofollow = getattr(os, "O_NOFOLLOW", None)
        directory = getattr(os, "O_DIRECTORY", None)
        if nofollow is None or directory is None:
            vlog("sidecar: safe no-follow file operations unavailable")
            failed += 1
            continue
        try:
            parent_identity = _directory_identity(os.fstat(parent_fd))
            if parent_identity is None:
                raise OSError("track parent is not a directory")
            track_flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
            track_flags |= getattr(os, "O_CLOEXEC", 0)
            track_fd = os.open(fp.name, track_flags, dir_fd=parent_fd)
            track_identity = _regular_file_identity(os.fstat(track_fd))
            if (
                track_identity is None
                or (expected_track is not None and track_identity != expected_track)
                or not _held_track_is_named(fp, parent_fd, track_fd, track_identity, parent_guard)
            ):
                raise OSError("track identity changed")

            def track_guard(
                fp=fp,
                parent_fd=parent_fd,
                track_fd=track_fd,
                track_identity=track_identity,
                parent_guard=parent_guard,
            ):
                return _held_track_is_named(
                    fp,
                    parent_fd,
                    track_fd,
                    track_identity,
                    parent_guard,
                )

            f = lyric_fetch.FLAC(f"/proc/self/fd/{track_fd}")
        except Exception as e:
            vlog(f"sidecar: FLAC open failed {fp}: {e}")
            failed += 1
            if track_fd is not None:
                os.close(track_fd)
            continue
        try:
            content = lyric_fetch.get_existing_lyrics(f)
            if not content or not content.strip():
                continue
            if not _held_track_is_named(fp, parent_fd, track_fd, track_identity, parent_guard):
                vlog(f"sidecar: track changed before write {fp}")
                failed += 1
                continue
            creation = {}
            sidecar_identity_receipt = {}
            sidecar_directory_mutation = {}
            sidecar_error = None
            try:
                if isinstance(created_files_out, list):
                    sidecar_written = lyric_fetch.write_sidecar(
                        fp,
                        content,
                        creation_out=creation,
                        directory_mutation_out=sidecar_directory_mutation,
                        parent_fd=parent_fd,
                        parent_guard=parent_guard,
                        track_guard=track_guard,
                        sidecar_identity_out=sidecar_identity_receipt,
                    )
                else:
                    sidecar_written = lyric_fetch.write_sidecar(
                        fp,
                        content,
                        parent_fd=parent_fd,
                        parent_guard=parent_guard,
                        track_guard=track_guard,
                        sidecar_identity_out=sidecar_identity_receipt,
                    )
            except OSError as e:
                sidecar_error = e
            finally:
                track_unchanged = _held_track_is_named(
                    fp, parent_fd, track_fd, track_identity, parent_guard
                )
                created = _created_sidecar_receipt(fp, parent_fd, track_identity, creation)
                if isinstance(created_files_out, list) and track_unchanged and created is not None:
                    created_files_out.append(created)
                    # Publication completed even if the following parent
                    # fsync reported an error. Carry the exact directory
                    # identity forward so Undo remains bound to the file
                    # that actually landed.
                    directory_mutated = True
                if sidecar_directory_mutation.get("mutated") is True:
                    directory_mutated = True
            if sidecar_error is not None:
                vlog(f"sidecar: write failed {fp}: {sidecar_error}")
                failed += 1
                continue
            if not track_unchanged:
                vlog(f"sidecar: track changed during write {fp}")
                failed += 1
                continue
            if sidecar_written:
                written += 1
                directory_mutated = True
            else:
                kept += 1

            if strip_tag:
                change = {}
                tag_directory_mutation = {}
                sidecar_fd = None
                sidecar_exclusion = None
                try:
                    sidecar = fp.with_suffix(".lrc")
                    sidecar_identity = _sidecar_identity_from_receipt(
                        fp, sidecar_identity_receipt
                    )
                    if sidecar_identity is None:
                        raise OSError(
                            f"{sidecar.name}: final sidecar identity wasn't proved"
                        )
                    sidecar_flags = os.O_RDONLY | nofollow
                    sidecar_flags |= getattr(os, "O_NONBLOCK", 0)
                    sidecar_flags |= getattr(os, "O_CLOEXEC", 0)
                    sidecar_fd = os.open(sidecar.name, sidecar_flags, dir_fd=parent_fd)
                    if not _held_track_is_named(
                        sidecar,
                        parent_fd,
                        sidecar_fd,
                        sidecar_identity,
                        parent_guard,
                    ):
                        raise OSError(f"{sidecar.name}: sidecar changed before tag strip")
                    sidecar_exclusion = acquire_inode_write_exclusion(sidecar_fd)
                    if sidecar_exclusion is None:
                        raise OSError(
                            f"{sidecar.name}: sidecar is open for writing or "
                            "cannot be protected"
                        )

                    def sidecar_guard(
                        sidecar=sidecar,
                        sidecar_fd=sidecar_fd,
                        sidecar_identity=sidecar_identity,
                        sidecar_exclusion=sidecar_exclusion,
                        parent_fd=parent_fd,
                        parent_guard=parent_guard,
                        track_guard=track_guard,
                    ):
                        return (
                            sidecar_exclusion.intact()
                            and track_guard() is True
                            and _held_track_is_named(
                                sidecar,
                                parent_fd,
                                sidecar_fd,
                                sidecar_identity,
                                parent_guard,
                            )
                        )

                    if not sidecar_guard():
                        raise OSError(f"{sidecar.name}: sidecar changed before tag strip")
                    had = False
                    for _k in ("lyrics", "unsyncedlyrics"):
                        if _k in f.tags:
                            del f.tags[_k]
                            had = True
                    if had:
                        if not _held_track_is_named(
                            fp, parent_fd, track_fd, track_identity, parent_guard
                        ):
                            raise OSError("track changed before tag rewrite")
                        if isinstance(identity_changes_out, list):
                            lyric_fetch.save_flac_tags(
                                f,
                                fp,
                                identity_change_out=change,
                                directory_mutation_out=(tag_directory_mutation),
                                parent_fd=parent_fd,
                                parent_guard=parent_guard,
                                expected_identity=track_identity,
                                commit_guard=sidecar_guard,
                            )
                        else:
                            lyric_fetch.save_flac_tags(
                                f,
                                fp,
                                parent_fd=parent_fd,
                                parent_guard=parent_guard,
                                expected_identity=track_identity,
                                commit_guard=sidecar_guard,
                            )
                        stripped += 1
                except Exception as e:
                    strip_failed += 1
                    action = "wrote" if sidecar_written else "kept"
                    log.info(
                        fmt(
                            C.YELLOW,
                            f"  ⚠  lyrics: {action} "
                            f"{fp.with_suffix('.lrc').name}, but couldn't "
                            f"safely remove embedded lyrics from "
                            f"{fp.name}: {e}.",
                        )
                    )
                finally:
                    if sidecar_exclusion is not None:
                        sidecar_exclusion.close()
                    if sidecar_fd is not None:
                        os.close(sidecar_fd)
                    if (
                        isinstance(identity_changes_out, list)
                        and change.get("path") == os.path.abspath(os.fspath(fp))
                        and _valid_file_identity(change.get("before"))
                        and _valid_file_identity(change.get("after"))
                        and change["before"] != change["after"]
                    ):
                        identity_changes_out.append(dict(change))
                        directory_mutated = True
                    if tag_directory_mutation.get("mutated") is True:
                        directory_mutated = True
        finally:
            if (
                isinstance(directory_changes_out, list)
                and directory_mutated
                and parent_identity is not None
            ):
                try:
                    after_parent = _directory_identity(os.fstat(parent_fd))
                    if (
                        after_parent is not None
                        and parent_guard() is True
                        and after_parent != parent_identity
                    ):
                        directory_changes_out.append(
                            {
                                "path": os.path.abspath(os.fspath(fp.parent)),
                                "before": dict(parent_identity),
                                "after": dict(after_parent),
                            }
                        )
                except OSError:
                    pass
            os.close(track_fd)
    ready = written + kept
    if ready or failed:
        actions = []
        if written:
            actions.append(f"wrote {written} .lrc sidecar(s)")
        if kept:
            actions.append(f"kept {kept} better existing .lrc sidecar(s)")
        if failed:
            noun = "sidecar" if failed == 1 else "sidecars"
            actions.append(f"{failed} {noun} failed")
        suffix = ""
        if strip_tag and ready:
            suffix = (
                " (embedded tags removed)"
                if stripped == ready
                else f" (embedded tag removal confirmed for {stripped} of {ready})"
            )
            if strip_failed:
                noun = "removal" if strip_failed == 1 else "removals"
                suffix += f"; {strip_failed} tag {noun} failed"
        warning = bool(failed or strip_failed)
        marker = "⚠" if warning else "✓"
        colour = C.YELLOW if warning else C.GREEN
        log.info(fmt(colour, f"  {marker}  lyrics: {'; '.join(actions)}{suffix}."))


def _record_post_import_lyric_retry(post_paths):
    """Update LYRIC_RETRY_FILE with post-import paths of transient files.
    Replaces the staging-keyed call to `_refresh_lyric_retry` from inside
    `_run_lyric_hook`.
    """
    # Load→modify→save under the manifest lock so a concurrent retry-run prune
    # in the other worker lane can't clobber the entries we add here.
    try:
        with _manifest_lock():
            existing = load_lyric_retry(fail_closed=True)
            fresh = [p for p in existing if Path(p).exists()]
            fresh = sorted(set(fresh) | set(post_paths))
            saved = _save_lyric_retry_unlocked(fresh)
            if not saved:
                log.info(fmt(
                    C.YELLOW,
                    "  ⚠  Lyrics retry paths couldn't be saved; the affected "
                    "tracks were not queued for the next run.",
                ))
                return False
            return True
    except (OSError, ValueError) as e:
        vlog(f"_record_post_import_lyric_retry: manifest unchanged ({e})")
        log.info(fmt(
            C.YELLOW,
            "  ⚠  Lyrics retry paths couldn't be saved; the affected tracks "
            "were not queued for the next run.",
        ))
        return False


def _prune_lyric_state_orphans():
    """Drop staging-path keys from lyric_fetch's state file -
    they're orphaned the moment beets moves the file out of staging.

    Done as one atomic read-modify-write under the state lock: a plain
    load→modify→save here can clobber an entry a concurrent import-hook
    checkpoint just added (a CLI run and the web worker write the same file),
    losing that track's lyric-retry bookkeeping."""
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


# ── Lyric retry manifest ──────────────────────────────────────────────────────


@contextmanager
def _manifest_lock():
    """Exclusive lock for a read-modify-write of the retry manifest. The two web
    worker lanes (an import-hook recording transient files, a retry run pruning
    resolved ones) both load→modify→save this file; without serialising, one's
    save clobbers the other's just-added entry. A lock failure blocks the
    mutation so the manifest cannot silently lose an entry."""
    with state_file.store_lock(cfg.LYRIC_RETRY_FILE):
        yield


def load_lyric_retry(*, fail_closed=False):
    """Return paths queued for retry, optionally blocking on read failure."""
    try:
        payload = state_file.load_json_object(
            cfg.LYRIC_RETRY_FILE,
            "the saved lyric retry queue",
            "your queued lyric retries",
        )
    except OSError as e:
        if fail_closed:
            raise
        log.info(fmt(
            C.YELLOW,
            f"  ⚠  {cfg.LYRIC_RETRY_FILE.name} could not be read ({e}); "
            "leaving it unchanged.",
        ))
        return []
    if payload is None:
        return []
    if payload.get("version") != cfg.LYRIC_RETRY_VERSION:
        reason = ValueError(
            f"{cfg.LYRIC_RETRY_FILE.name} version "
            f"{payload.get('version')!r} is not supported by this build"
        )
        if fail_closed:
            raise reason
        log.info(fmt(C.YELLOW, f"  ⚠  {reason}; leaving it unchanged."))
        return []
    files = payload.get("files") or []
    if not isinstance(files, list):
        reason = ValueError(
            f"{cfg.LYRIC_RETRY_FILE.name} has a malformed files list"
        )
        if fail_closed:
            raise reason
        log.info(fmt(C.YELLOW, f"  ⚠  {reason}; leaving it unchanged."))
        return []
    # Drop anything non-string in case of corruption.
    return [f for f in files if isinstance(f, str)]


def _save_lyric_retry_unlocked(paths):
    paths = sorted({p for p in paths if isinstance(p, str) and p})
    if not paths:
        try:
            cfg.LYRIC_RETRY_FILE.unlink(missing_ok=True)
            return True
        except OSError as e:
            vlog(f"clear_lyric_retry: {e}")
            return False
    try:
        payload = {
            "version": cfg.LYRIC_RETRY_VERSION,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "files": paths,
        }
        state_file.write_json(cfg.LYRIC_RETRY_FILE, payload)
        return True
    except OSError as e:
        vlog(f"save_lyric_retry failed: {e}")
        return False


def save_lyric_retry(paths):
    """Persist paths under the manifest lock; an empty list clears it."""
    try:
        with _manifest_lock():
            return _save_lyric_retry_unlocked(paths)
    except OSError as e:
        vlog(f"save_lyric_retry failed: {e}")
        return False


def _refresh_lyric_retry(flacs_just_processed):
    """After a lyric_fetch run, reconcile the manifest with current state.

    Reads lyric_fetch's state file directly. The manifest will end up containing
    exactly those files (from the union of prior-manifest entries and the
    just-processed flacs) whose state.status is currently "transient" or whose
    persisted path failed the owned-library boundary. Resolved files (synced,
    plain, not_found, skipped, error) drop off naturally; unsafe entries stay
    visible for explicit review instead of being reported as resolved.
    """
    if not HAVE_LYRIC_FETCH:
        return False
    try:
        # Late import to avoid pulling lyric helpers in at module load.
        state = lyric_fetch.load_state(cfg.LYRIC_FETCH_STATE_FILE)
    except Exception as e:
        vlog(f"_refresh_lyric_retry: couldn't load lyric state: {e}")
        return False

    # Read→reconcile→save under the manifest lock so the read here and the save
    # below can't straddle a concurrent import-hook record (which would drop the
    # entry that hook just added). The just-processed flacs are merged back in
    # regardless, so they're never lost.
    try:
        with _manifest_lock():
            candidates = set(load_lyric_retry(fail_closed=True))
            candidates.update(str(p) for p in (flacs_just_processed or []))

            still_transient = []
            for p in candidates:
                st = state.get(p)
                if st is None:
                    # File no longer tracked (maybe deleted, maybe state was reset);
                    # drop it from the manifest.
                    continue
                if st.status in ("transient", "unsafe_path"):
                    still_transient.append(p)

            return _save_lyric_retry_unlocked(still_transient)
    except (OSError, ValueError) as e:
        vlog(f"_refresh_lyric_retry: manifest unchanged ({e})")
        return False


def offer_resume_lyric_retry(args):
    """Startup hook. If files are queued for a lyric retry, prompt to retry
    now / keep for later / discard. Mirrors offer_resume_pending_queue."""
    paths = load_lyric_retry()
    if not paths:
        return False
    if not lyric_fetch.AVAILABLE:
        log.info(
            fmt(
                C.YELLOW,
                f"  ⚠  {len(paths)} file(s) queued for lyric retry but the "
                f"syncedlyrics provider library isn't installed. Manifest preserved.",
            )
        )
        return False

    # Filter out files that have since vanished (manual move/delete).
    existing = [p for p in paths if Path(p).exists()]
    missing = len(paths) - len(existing)
    if not existing:
        log.info(
            fmt(
                C.GRAY,
                f"  All {len(paths)} file(s) in {cfg.LYRIC_RETRY_FILE.name} "
                f"are missing on disk; clearing manifest.",
            )
        )
        if save_lyric_retry([]):
            log.info(fmt(C.GRAY, "  Lyric retry manifest cleared."))
        else:
            log.info(fmt(
                C.YELLOW,
                "  ⚠  Lyric retry manifest couldn't be cleared; it was left "
                "unchanged and may be offered again.",
            ))
        return False

    print()
    log.info(
        fmt(
            C.BOLD + C.YELLOW,
            f"  ⚠  Lyric retry pending: {len(existing)} file(s) had providers "
            f"unavailable last run.",
        )
    )
    if missing:
        log.info(
            fmt(
                C.GRAY,
                f"     ({missing} more in the manifest no longer exist on disk; will be dropped.)",
            )
        )
    log.info(fmt(C.GRAY, f"     File: {cfg.LYRIC_RETRY_FILE}"))
    log.info(fmt(C.GRAY, "     These are NOT 'no lyrics exist' - those would already be marked"))
    log.info(fmt(C.GRAY, "     not-found and skipped here. These are files where every provider"))
    log.info(fmt(C.GRAY, "     was rate-limited or unreachable when their turn came up."))
    print()

    while True:
        try:
            ans = (
                input(fmt(C.CYAN, "  Retry lyrics now? [Y]es / [k]eep for later / [d]iscard: "))
                .strip()
                .lower()
            )
        except EOFError:
            ans = ""
        if ans in ("", "y", "yes"):
            log.info(fmt(C.CYAN, f"\n  ⟳  Retrying lyrics on {len(existing)} file(s)…"))
            counts = {}
            try:
                paths_obj = [Path(p) for p in existing]
                counts = lyric_fetch.fetch_for_paths(
                    paths_obj,
                    owned_root=cfg.MUSIC_ROOT,
                    log=log,
                    providers=cfg.LYRICS_PROVIDERS or None,
                    lyrics_format=cfg.LYRICS_FORMAT,
                    state_path=cfg.LYRIC_FETCH_STATE_FILE,
                )
            except KeyboardInterrupt:
                log.info(
                    fmt(
                        C.YELLOW,
                        "\n  ⚠  Retry interrupted; manifest will be refreshed from current state.",
                    )
                )
            except Exception as e:
                log.info(fmt(C.YELLOW, f"  ⚠  Retry failed: {e}; manifest preserved."))
                return False
            # Reconcile manifest against fresh state - resolved files drop,
            # still-transient stay.
            refreshed = _refresh_lyric_retry(paths_obj)
            remaining = load_lyric_retry()
            attempted_paths = {str(path) for path in paths_obj}
            attempted_remaining = sum(
                1 for path in remaining if path in attempted_paths
            )
            outcome = summarize_lyric_retry(
                counts,
                attempted=len(existing),
                remaining=attempted_remaining,
            )
            if not refreshed:
                log.info(fmt(
                    C.YELLOW,
                    f"  ⚠  Retry processed {len(existing)} file(s), but the "
                    "saved retry queue couldn't be updated; it was left "
                    "unchanged and will be offered again.",
                ))
                return False
            if outcome["failed"]:
                log.info(
                    fmt(
                        C.YELLOW,
                        f"  ⚠  Retry finished: {outcome['resolved']} resolved; "
                        f"{outcome['failed']} failed and need review; "
                        f"{outcome['remaining']} still queued for retry.",
                    )
                )
            elif remaining:
                log.info(
                    fmt(
                        C.YELLOW,
                        f"  ⚠  {len(remaining)} file(s) still need review or "
                        "retry. Will prompt next launch.",
                    )
                )
            else:
                log.info(fmt(C.GREEN, "  ✓  All retried files resolved."))
            return False  # fall through to menu
        if ans in ("k", "keep"):
            log.info(
                fmt(C.GRAY, "  Keeping the lyric retry manifest. It'll prompt again next launch.")
            )
            return False
        if ans in ("d", "discard"):
            try:
                conf = input(
                    fmt(
                        C.YELLOW,
                        f"  Really discard {len(existing)} retry-queued file(s)? "
                        "Type DISCARD to confirm: ",
                    )
                ).strip()
            except EOFError:
                conf = ""
            if conf == "DISCARD":
                if save_lyric_retry([]):
                    log.info(fmt(C.GRAY, "  Lyric retry manifest cleared."))
                else:
                    log.info(fmt(
                        C.YELLOW,
                        "  ⚠  Lyric retry manifest couldn't be cleared; it "
                        "was left unchanged.",
                    ))
            else:
                log.info(fmt(C.GRAY, "  Discard cancelled."))
            return False
        log.info(fmt(C.GRAY, "  Enter Y, k, or d."))
