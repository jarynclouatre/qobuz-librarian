"""Tell whether a held regular file was written since it was checked."""

from __future__ import annotations

import os
import stat
import weakref


def _written_identity(value):
    # A write moves the size or mtime. ctime is left out because the app's
    # own renames move it too; callers compare it before they act.
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


class InodeWriteExclusion:
    """The identity a held file had when it was checked."""

    def __init__(self, descriptor: int):
        held = os.dup(descriptor)
        self._close = weakref.finalize(self, os.close, held)
        self._descriptor = held
        self._identity = _written_identity(os.fstat(held))

    def intact(self) -> bool:
        if not self._close.alive:
            return False
        try:
            return _written_identity(os.fstat(self._descriptor)) == self._identity
        except OSError:
            return False

    def close(self) -> None:
        self._close()

    def __enter__(self) -> InodeWriteExclusion:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def acquire_inode_write_exclusion(descriptor: int) -> InodeWriteExclusion | None:
    """Start watching a held regular file, or None when it is not one."""
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        return InodeWriteExclusion(descriptor)
    except OSError:
        return None
