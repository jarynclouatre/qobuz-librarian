"""File operations the app's safety code shares.

Linux only: renames go through renameat2(2), locks through flock(2).
"""
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import secrets
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2
AT_FDCWD = -100


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


def copy_metadata(source_fd, destination_fd) -> None:
    """Give the copy its source's mode and times, and its owner, group and
    xattrs, ACLs included, where the app may set them."""
    value = os.fstat(source_fd)
    try:
        os.fchown(destination_fd, value.st_uid, value.st_gid)
    except OSError:
        # Without the owner the group still matters: a media server may
        # read the music through it.
        try:
            os.fchown(destination_fd, -1, value.st_gid)
        except OSError:
            pass
    os.fchmod(destination_fd, stat.S_IMODE(value.st_mode))
    try:
        names = os.listxattr(source_fd)
    except OSError:
        names = ()
    for name in names:
        try:
            os.setxattr(destination_fd, name, os.getxattr(source_fd, name))
        except OSError:
            pass
    os.utime(destination_fd, ns=(value.st_atime_ns, value.st_mtime_ns))


def copy_file(source, destination, *, sha256=None) -> str:
    """Copy the file `source` to the new name `destination` and return the
    sha256 of its bytes.

    The bytes go to a hidden temp name beside `destination`, synced, and
    are renamed to it without replacing anything: FileExistsError when the
    name is taken. A link is never followed at either end. Refuses before
    the rename when the source changed while it was read, or its bytes do
    not have the `sha256` given. A stop part way leaves at most the temp,
    named `.ql-copy-` and 32 hex digits."""
    destination = os.fspath(destination)
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                        | os.O_CLOEXEC)
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise OSError(errno.EINVAL, "not a regular file", os.fspath(source))
        temporary = os.path.join(os.path.dirname(destination),
                                 f".ql-copy-{secrets.token_hex(16)}")
        target_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                            | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            try:
                digest = hashlib.sha256()
                offset = 0
                while chunk := os.pread(source_fd, 1024 * 1024, offset):
                    write_all(target_fd, chunk)
                    digest.update(chunk)
                    offset += len(chunk)
                copy_metadata(source_fd, target_fd)
                os.fsync(target_fd)
            finally:
                os.close(target_fd)
            if stat_identity(os.fstat(source_fd)) != stat_identity(before):
                raise OSError(errno.EAGAIN, "changed while it was copied",
                              os.fspath(source))
            if sha256 is not None and digest.hexdigest() != sha256:
                raise OSError(errno.EIO, "copy differs from its record",
                              os.fspath(source))
            rename_noreplace(AT_FDCWD, temporary, AT_FDCWD, destination)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    finally:
        os.close(source_fd)
    fsync_dir(os.path.dirname(destination))
    return digest.hexdigest()


def _raise(error):
    raise error


def snapshot_tree(root):
    """Every entry below `root` by relative path, with its stat_identity.

    Taken with lstat: a link is recorded as a link and never followed. None
    when `root` is not a folder itself or part of it cannot be read."""
    snapshot = {}
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            return None
        for parent, folders, files in os.walk(root, onerror=_raise):
            for name in (*folders, *files):
                path = os.path.join(parent, name)
                snapshot[os.path.relpath(path, root)] = stat_identity(
                    os.lstat(path))
    except OSError:
        return None
    return snapshot


def fsync_tree(root) -> bool:
    """Flush every regular file below `root` to disk, then every folder from
    `root` down and the folder holding it. Links are not followed. False
    when any of it fails."""
    try:
        for parent, _folders, files in os.walk(root, onerror=_raise):
            for name in files:
                path = os.path.join(parent, name)
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    continue
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW
                                     | os.O_NONBLOCK | os.O_CLOEXEC)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            fsync_dir(parent, strict=True)
        fsync_dir(os.path.dirname(os.path.abspath(root)), strict=True)
    except OSError:
        return False
    return True


def delete_verified(path, expected, *, sync=True) -> bool:
    """Unlink `path` while lstat still gives the `expected` stat_identity.

    True once the name is gone, also when it was already missing."""
    try:
        if stat_identity(os.lstat(path)) != tuple(expected):
            return False
        os.unlink(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if sync:
        fsync_dir(os.path.dirname(os.fspath(path)))
    return True


def delete_tree_verified(root, snapshot) -> list[str]:
    """Delete what below `root` still matches `snapshot`, then each folder
    that is left empty, deepest first, and `root` itself.

    Hidden files go last in each folder and stay while anything else is
    left there, so a folder keeps its own metadata with what is left. A file
    added or changed since the snapshot stays, and so does every folder
    above it. A file with another name in the tree may have a new ctime
    once that name is gone, and then needs the bytes it had. Returns the
    relative paths left, "." for `root`."""
    root = os.fspath(root)
    left = []
    names = {}
    for relative, value in snapshot.items():
        if not stat.S_ISDIR(value[2]):
            names.setdefault(tuple(value[:2]), []).append(relative)
    # The bytes of each file with more names here, read before the first
    # goes: unlinking it moves the others' ctime, so they are matched on
    # these instead.
    digests = {}
    for relative in sorted(snapshot, key=lambda name: (
            -name.count(os.sep), os.path.basename(name).startswith("."))):
        path = os.path.join(root, relative)
        expected = snapshot[relative]
        key = tuple(expected[:2])
        if os.path.basename(relative).startswith(".") and _holds_others(
                root, os.path.dirname(relative), snapshot, left):
            removed = False
        elif stat.S_ISDIR(expected[2]):
            removed = _remove_dir(path)
        else:
            if len(names[key]) > 1 and key not in digests:
                digests[key] = _digest_of(path, expected)
            elif len(names[key]) > 1:
                try:
                    current = stat_identity(os.lstat(path))
                except OSError:
                    current = None
                if (
                    current is not None
                    and current[:5] == tuple(expected[:5])
                    and digests[key] is not None
                    and _digest_of(path, current) == digests[key]
                ):
                    expected = current
            removed = delete_verified(path, expected, sync=False)
            if not removed and key in digests:
                digests[key] = None
        if not removed:
            left.append(relative)
    if not _remove_dir(root):
        left.append(".")
    fsync_dir(os.path.dirname(root))
    return sorted(left)


def _digest_of(path, expected):
    """The sha256 of the file at `path` while it has the `expected`
    stat_identity, else None."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW
                             | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if stat_identity(os.fstat(descriptor)) != tuple(expected):
            return None
        return digest_fd(descriptor)
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _holds_others(root, folder, snapshot, left) -> bool:
    """Whether `folder` holds anything but hidden entries of the snapshot,
    or keeps anything at any depth below it."""
    inside = os.path.join(folder, "") if folder else ""
    if any(kept.startswith(inside) for kept in left):
        return True
    try:
        names = os.listdir(os.path.join(root, folder))
    except OSError:
        return True
    return any(not name.startswith(".")
               or os.path.join(folder, name) not in snapshot
               for name in names)


def _remove_dir(path) -> bool:
    try:
        os.rmdir(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True
