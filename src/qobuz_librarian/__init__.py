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
