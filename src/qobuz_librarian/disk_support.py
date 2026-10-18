"""What the disk under each storage folder can do, and the features that
cannot run on it.

A process that holds the library tries each folder once with a few tiny
hidden files that are removed straight after, and the answer is kept while
the folder is the same one. A folder that could not be tried refuses
nothing.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
import threading
import time
from typing import NamedTuple

from qobuz_librarian import config as cfg
from qobuz_librarian import run_lock, safefile


class Support(NamedTuple):
    noreplace: bool
    exchange: bool
    identity: bool


_LACKS = {
    "noreplace": "can't rename files safely",
    "exchange": "can't swap a file for its new copy in one step",
    "identity": "doesn't record when folders were created",
    "exact_times": "doesn't keep exact file times and permissions",
}


def _lyric_needs():
    # A new .lrc file only needs the safe rename; a track's tags are
    # rewritten by swapping in its new copy.
    from qobuz_librarian.integrations.lyric_fetch import _normalise_lyrics_format

    if _normalise_lyrics_format(cfg.LYRICS_FORMAT) == "sidecar":
        return {"music": ("noreplace",)}
    return {"music": ("noreplace", "exchange")}


# Each feature: how its refusal starts, what Diagnostics calls it, and what
# it needs of each folder.
_FEATURES = {
    "downloads": ("Downloads need", ("downloads",), {
        "staging": ("noreplace", "exchange")}),
    "whole_albums": (
        "This album downloads in one crash-safe step, which needs",
        ("crash-safe whole-album downloads",), {"music": ("noreplace",)}),
    "upgrade": ("Upgrade needs", ("Upgrade",), {
        "staging": ("noreplace", "exchange"),
        "music": ("noreplace", "exchange", "identity"),
        "backups": ("noreplace",)}),
    "repair": ("Repair needs", ("Repair",), {
        "music": ("noreplace",), "backups": ("noreplace",)}),
    "replacements": (None, ("replacing tracks in Gap Fill or Consolidation",), {
        "music": ("noreplace",), "backups": ("noreplace",)}),
    "lyrics": ("Lyrics need", ("Lyrics",), _lyric_needs),
    "downsample": ("Downsample needs", ("Downsample",), {
        "music": ("exchange",)}),
    "lyric_carry": ("Copying a backup's lyrics onto the album needs", (),
                    _lyric_needs),
    "lrc_replace": ("Replacing an album's .lrc file with a backup's needs", (),
                    {"music": ("exchange",)}),
    "picks": ("Albums picked from a scan need", ("albums picked from a scan",), {
        "music": ("identity",)}),
    "album_changes": (
        "Changing an album already in the library needs",
        ("changes to albums already in the library",), {"music": ("identity",)}),
}

_UNSUPPORTED = (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP)
_REFUSED = (*_UNSUPPORTED, errno.EPERM)
_PREFIX = ".ql-disk-check-"
_STALE_SECONDS = 600

_cache: dict = {}
_cache_lock = threading.Lock()


def is_probe_name(name) -> bool:
    """Whether ``name`` is one of the check's own files."""
    return str(name).startswith(_PREFIX)


def _folders():
    return {
        "music": ("the music folder", "Music library", cfg.MUSIC_ROOT),
        "staging": ("the staging folder", "Staging area", cfg.STAGING_DIR),
        "backups": ("the upgrade backup folder", "Upgrade backups",
                    cfg.UPGRADE_BACKUP_DIR),
    }


def _needs(feature) -> dict:
    needs = _FEATURES[feature][2]
    return needs() if callable(needs) else needs


def _works(call, *args) -> bool:
    """Whether ``call`` works here. Any error but "not supported" is raised,
    so the folder counts as not tried."""
    try:
        call(*args)
    except OSError as exc:
        if exc.errno is None or exc.errno in _UNSUPPORTED:
            return False
        raise
    return True


def _create(folder_fd, name) -> int:
    return os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                   | os.O_CLOEXEC, 0o600, dir_fd=folder_fd)


def _sweep(folder_fd) -> None:
    """Remove check files that a stopped earlier check left behind."""
    try:
        with os.scandir(folder_fd) as listing:
            names = [entry.name for entry in listing if is_probe_name(entry.name)]
    except OSError:
        return
    for name in names:
        try:
            value = os.stat(name, dir_fd=folder_fd, follow_symlinks=False)
            if (stat.S_ISREG(value.st_mode)
                    and time.time() - value.st_ctime > _STALE_SECONDS):
                os.unlink(name, dir_fd=folder_fd)
        except OSError:
            pass


def _probe(path) -> Support | None:
    from qobuz_librarian.library.migrate import _directory_identity

    try:
        folder_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return None
    first, second, third = (_PREFIX + secrets.token_hex(16) for _ in range(3))
    try:
        _sweep(folder_fd)
        identity = _works(_directory_identity, folder_fd)
        os.close(_create(folder_fd, first))
        noreplace = _works(safefile.rename_noreplace, folder_fd, first,
                           folder_fd, second)
        os.close(_create(folder_fd, third))
        exchange = _works(safefile.rename_exchange, folder_fd,
                          second if noreplace else first, folder_fd, third)
        return Support(noreplace, exchange, identity)
    except OSError:
        return None
    finally:
        for name in (first, second, third):
            try:
                os.unlink(name, dir_fd=folder_fd)
            except OSError:
                pass
        os.close(folder_fd)


def support(folder) -> Support | None:
    """What the disk under ``folder`` can do, or None when it could not be
    tried. Tried again only when the path names another folder."""
    path = os.path.abspath(os.fspath(folder))
    try:
        value = os.stat(path)
    except OSError:
        return None
    key = (value.st_dev, value.st_ino)
    with _cache_lock:
        cached = _cache.get(path)
    if cached is not None and cached[0] == key:
        return cached[1]
    if run_lock.current_lease() is None:
        return None
    found = _probe(path)
    if found is not None:
        with _cache_lock:
            _cache[path] = (key, found)
    return found


def _joined(parts) -> str:
    parts = list(parts)
    return parts[0] if len(parts) == 1 else (
        ", ".join(parts[:-1]) + " and " + parts[-1])


def _lacking(path, wanted) -> list[str]:
    found = support(path)
    if found is None:
        return []
    return [_LACKS[need] for need in wanted if not getattr(found, need)]


def refusal(*features) -> str | None:
    """Why the first of ``features`` that cannot run here cannot, or None."""
    folders = _folders()
    for feature in features:
        start = _FEATURES[feature][0]
        for key, wanted in _needs(feature).items():
            label, _title, path = folders[key]
            lacks = _lacking(path, wanted)
            if lacks:
                return f"{start} {label} on a local disk: {path} {_joined(lacks)}."
    return None


def _keeps_times(folder, sample) -> bool | None:
    """Whether a file in ``folder`` keeps the mode and modification time of
    ``sample``, a source file's stat, as Migrate's copies must. None when it
    could not be tried."""
    if run_lock.current_lease() is None:
        return None
    mode = stat.S_IMODE(sample.st_mode)
    try:
        folder_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return None
    name = _PREFIX + secrets.token_hex(16)
    try:
        descriptor = _create(folder_fd, name)
        try:
            try:
                os.fchmod(descriptor, mode)
                os.utime(descriptor, ns=(os.fstat(descriptor).st_atime_ns,
                                         sample.st_mtime_ns))
            except OSError as exc:
                return False if exc.errno in _REFUSED else None
            value = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        return (value.st_mtime_ns == sample.st_mtime_ns
                and stat.S_IMODE(value.st_mode) == mode)
    except OSError:
        return None
    finally:
        try:
            os.unlink(name, dir_fd=folder_fd)
        except OSError:
            pass
        os.close(folder_fd)


def destination_refusal(path, sample=None) -> str | None:
    """Why Migrate cannot copy into the existing folder ``path``, or None.
    ``sample`` is the stat of a source file whose mode and time a copy keeps."""
    lacks = _lacking(path, ("noreplace", "identity"))
    if sample is not None and _keeps_times(path, sample) is False:
        lacks.append(_LACKS["exact_times"])
    if not lacks:
        return None
    return f"Migrate needs a destination on a local disk: {path} {_joined(lacks)}."


def report(display=str) -> list[tuple[str, bool, str]]:
    """(label, ok, detail) for each storage folder whose disk was tried,
    with each path shown through ``display``."""
    rows = []
    needs = {feature: _needs(feature) for feature in _FEATURES}
    for key, (_label, title, path) in _folders().items():
        found = support(path)
        if found is None:
            continue
        wanted = {need for feature in _FEATURES
                  for need in needs[feature].get(key, ())}
        lacks = [need for need in Support._fields
                 if need in wanted and not getattr(found, need)]
        affected = [name for feature, (_start, names, _needs) in _FEATURES.items()
                    if set(lacks) & set(needs[feature].get(key, ()))
                    for name in names]
        if not lacks:
            rows.append((f"{title} disk", True,
                         "Everything the app does here works"))
            continue
        rows.append((f"{title} disk", False,
                     f"{display(path)} {_joined(_LACKS[need] for need in lacks)}, "
                     f"so {_joined(affected)} can't run on it. Use a local "
                     "disk for it."))
    return rows
