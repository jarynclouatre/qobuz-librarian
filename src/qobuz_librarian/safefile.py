"""File operations the app's safety code shares.

Linux only: renames go through renameat2(2), locks through flock(2).
"""
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2


def renameat2(source_fd, source_name, destination_fd, destination_name, flags):
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable") from exc
    rename.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename.restype = ctypes.c_int
    ctypes.set_errno(0)
    if rename(
        int(source_fd),
        os.fsencode(source_name),
        int(destination_fd),
        os.fsencode(destination_name),
        int(flags),
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), os.fspath(destination_name))


def rename_noreplace(source_fd, source_name, destination_fd, destination_name):
    """Rename, refusing with FileExistsError when the destination exists."""
    renameat2(source_fd, source_name, destination_fd, destination_name,
              RENAME_NOREPLACE)


def rename_exchange(source_fd, source_name, destination_fd, destination_name):
    """Swap two existing names in one step."""
    renameat2(source_fd, source_name, destination_fd, destination_name,
              RENAME_EXCHANGE)


def digest_fd(descriptor) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)
        offset += len(chunk)


def stat_identity(value):
    """What changes when a file is replaced or written: device, inode, mode,
    size, mtime and ctime."""
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def identity_record(value) -> dict[str, int]:
    """The same identity as a JSON record, for intents and receipts."""
    return {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "size": int(value.st_size),
        "modified_ns": int(value.st_mtime_ns),
        "changed_ns": int(value.st_ctime_ns),
    }


def named_entry_missing(parent_fd, name) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return False


def named_entry_matches(parent_fd, name, descriptor, *, directory=None) -> bool:
    """Whether `name` in `parent_fd` is still the inode `descriptor` holds.

    `directory=True` also requires a folder, `directory=False` a regular
    file."""
    try:
        held = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except (OSError, TypeError, ValueError):
        return False
    if directory is not None:
        kind = stat.S_ISDIR if directory else stat.S_ISREG
        if not kind(held.st_mode):
            return False
    return (
        stat.S_IFMT(held.st_mode), int(held.st_dev), int(held.st_ino)
    ) == (
        stat.S_IFMT(named.st_mode), int(named.st_dev), int(named.st_ino)
    )


def same_directory(left, right) -> bool:
    return (
        stat.S_ISDIR(left.st_mode)
        and stat.S_ISDIR(right.st_mode)
        and (int(left.st_dev), int(left.st_ino))
        == (int(right.st_dev), int(right.st_ino))
    )


def fsync_dir(path, *, strict=False) -> bool:
    """Make a create, rename or unlink inside `path` durable.

    A filesystem that cannot sync folders at all counts as done. Any other
    failure raises when `strict`, and otherwise returns False."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.ENOTSUP):
            return True
        if strict:
            raise
        return False
    return True


def fsync_dirs(*descriptors) -> None:
    """Fsync each distinct folder among the open descriptors once."""
    synced = set()
    for descriptor in descriptors:
        if descriptor is None:
            continue
        current = os.fstat(descriptor)
        if not stat.S_ISDIR(current.st_mode):
            raise OSError(errno.ENOTDIR, "not a directory")
        identity = (int(current.st_dev), int(current.st_ino))
        if identity in synced:
            continue
        os.fsync(descriptor)
        synced.add(identity)


@contextmanager
def exclusive_lock(path, *, dir_fd=None):
    """Hold an exclusive flock on the lock file `path`, creating it.

    The web worker and the terminal are separate processes, so a threading
    lock cannot serialize them. Fails before the caller goes on if the name
    is not one plain file, or was swapped while the lock was waited for."""
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        0o600,
        dir_fd=dir_fd,
    )
    try:
        held = os.fstat(descriptor)
        if (
            not stat.S_ISREG(held.st_mode)
            or held.st_nlink != 1
            or not named_entry_matches(dir_fd, path, descriptor)
        ):
            raise OSError(f"lock file {os.fspath(path)} is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            if (
                os.stat(path, dir_fd=dir_fd, follow_symlinks=False).st_nlink != 1
                or not named_entry_matches(dir_fd, path, descriptor)
            ):
                raise OSError(f"lock file {os.fspath(path)} changed while it "
                              "was waited for")
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def write_all(descriptor, data) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, "write made no progress")
        view = view[written:]


def write_new_at(dir_fd, name, data, *, mode=0o600) -> None:
    """Create `name` with `data`, synced; FileExistsError if it exists."""
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        mode,
        dir_fd=dir_fd,
    )
    try:
        write_all(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_atomic(path, data, *, mode=0o600, prefix=None, strict=False):
    """Replace `path` with `data`: text, bytes, or an iterable of either.

    A unique temp file in the same folder, synced before the rename: a fixed
    temp name lets two processes rename each other's half-written file into
    place, and an unsynced rename can leave an empty file after a power cut.
    The folder is synced after; `strict` raises when that fails. Returns the
    new file's stat. Raises OSError, leaving no temp behind."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=path.name + "." if prefix is None else prefix,
        suffix=".tmp",
    )
    try:
        try:
            os.fchmod(descriptor, mode)
            if isinstance(data, (str, bytes, bytearray, memoryview)):
                data = [data]
            with open(descriptor, "wb", closefd=False) as f:
                for chunk in data:
                    f.write(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
            os.fsync(descriptor)
            written = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    fsync_dir(path.parent, strict=strict)
    return written


def write_json(path, data, *, indent=2, ensure_ascii=True, separators=None,
               **options):
    """Replace `path` with `data` as JSON, as write_atomic does."""
    return write_atomic(path, json.JSONEncoder(
        ensure_ascii=ensure_ascii, indent=indent,
        separators=separators).iterencode(data), **options)
