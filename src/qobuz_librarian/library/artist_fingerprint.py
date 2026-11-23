"""Cheap local fingerprint for an artist folder.

The library refresh uses this to skip unchanged artist folders after a full
baseline exists. It deliberately uses only local file facts, not audio parsing,
so the check stays fast on large libraries and network mounts. Every file
counts, not only audio, and its ctime with it, because a review's receipt
covers the whole album folder and only a refresh rebuilds it.
"""
import hashlib
import stat
from pathlib import Path

from qobuz_librarian.library.scanner import iter_tree_no_symlinks


def artist_fingerprint(artist_dir: Path) -> str:
    """Return a stable signature for the files under ``artist_dir``."""
    h = hashlib.sha256()
    rows: list[tuple[str, int, int, int]] = []

    for path in iter_tree_no_symlinks(artist_dir):
        try:
            st = path.stat()
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            rows.append((path.relative_to(artist_dir).as_posix(),
                         int(st.st_mtime_ns), int(st.st_size),
                         int(st.st_ctime_ns)))

    for rel, mtime_ns, size, ctime_ns in sorted(rows):
        h.update(rel.encode("utf-8", "surrogateescape"))
        h.update(b"\0")
        h.update(str(mtime_ns).encode("ascii"))
        h.update(b"\0")
        h.update(str(size).encode("ascii"))
        h.update(b"\0")
        h.update(str(ctime_ns).encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()
