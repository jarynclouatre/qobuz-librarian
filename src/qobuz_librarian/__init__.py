"""Qobuz Librarian - album/artist downloader and music-library maintenance tool."""
from importlib.metadata import version

try:
    # Installed metadata, built from pyproject.toml. Both interfaces read this
    # one value so `--version` and the web UI sidebar can't disagree.
    __version__ = version("qobuz-librarian")
except Exception:
    # Only reached on a broken / non-installed run; "unknown" is honest, a
    # hardcoded number here just goes stale on the next bump.
    __version__ = "unknown"


def raise_open_file_limit(target: int = 65536) -> None:
    """Lift the soft open-file limit toward what the system already allows.

    Sealing a receipt holds a guard per file for a whole artist folder, so at
    the 1,024 soft limit a container inherits by default a large library
    leaves artists unchecked. Compose asks for a higher limit, but an install
    that predates it, or one started any other way, does not get it. Raising
    the soft limit needs no privilege; the hard limit is left alone.
    """
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        wanted = target if hard == resource.RLIM_INFINITY else min(target, hard)
        if soft == resource.RLIM_INFINITY or soft >= wanted:
            return
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
    except (ImportError, OSError, ValueError):
        # Nothing here is worth failing a start over: the scan still runs and
        # reports whatever it could not check.
        pass
