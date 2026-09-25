"""Descriptor-relative file operations."""
import ctypes
import errno
import hashlib
import os
import stat


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
    renameat2(source_fd, source_name, destination_fd, destination_name, 1)


def digest_fd(descriptor) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)
        offset += len(chunk)


def named_entry_missing(parent_fd, name) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return False


def named_entry_matches(parent_fd, name, descriptor) -> bool:
    try:
        held = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return (
            stat.S_IFMT(held.st_mode), int(held.st_dev), int(held.st_ino)
        ) == (
            stat.S_IFMT(named.st_mode), int(named.st_dev), int(named.st_ino)
        )
    except (OSError, TypeError, ValueError):
        return False


def same_directory(left, right) -> bool:
    return (
        stat.S_ISDIR(left.st_mode)
        and stat.S_ISDIR(right.st_mode)
        and (int(left.st_dev), int(left.st_ino))
        == (int(right.st_dev), int(right.st_ino))
    )
