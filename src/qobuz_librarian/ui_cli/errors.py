"""Error-message and copy helpers shared across CLI/web."""
import errno
import os
import sys

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import DownloadsRefused
from qobuz_librarian.ui_cli.colors import block

# Exit-code contract for the CLI.
EXIT_GENERAL    = 1   # unspecified failure (default for crashes/unknown)
EXIT_AUTH       = 2   # token invalid / expired
EXIT_LOCK_BUSY  = 3   # another writer holds the lock
EXIT_TRANSIENT  = 4   # network / Qobuz API trouble - retry later
EXIT_CONFIG     = 64  # config missing / unreadable / tool absent
# Ctrl-C. 128 + SIGINT, the shell convention, so a wrapper script can
# tell "the operator stopped it" from "it failed" without a scheme of
# our own. Nothing else moves off EXIT_GENERAL.
EXIT_INTERRUPT  = 130


def die(msg: str, code: int = EXIT_GENERAL):
    """Print msg to stderr, wrapped to the terminal, and exit with the code."""
    print(block(msg), file=sys.stderr)
    sys.exit(code)


def plural(n: int, singular: str, plural_form: str | None = None) -> str:
    """Format `n singular` / `n plural` with auto pluralization."""
    word = singular if n == 1 else (plural_form or singular + "s")
    return f"{n} {word}"


def shown_path(path) -> str:
    """A folder as the user finds it: under a bind mount, the host folder it
    comes from. The data volume has no host folder to name."""
    shown, on_host = cfg.host_path(path)
    if on_host or not cfg.in_container():
        return shown
    data = str(cfg.DATA_DIR).rstrip("/")
    if shown == data or shown.startswith(data + "/"):
        return "the app's data folder"
    return shown


def compose_service_name() -> str:
    """Best-effort docker compose service name for diagnostic hints."""
    h = os.environ.get("HOSTNAME", "").strip()
    if not h or (len(h) == 12 and all(c in "0123456789abcdef" for c in h)):
        return "qobuz-librarian"
    return h


def cli_command(options, *, interactive=True) -> str:
    """The command that runs the CLI with ``options`` on this install."""
    if not cfg.in_container():
        return f"qobuz-librarian {options}"
    tty = "-it " if interactive else ""
    return f"docker compose run --rm {tty}{compose_service_name()} cli {options}"


def no_artist_folders() -> str:
    """Said when the music folder holds no artist folders."""
    setting = ("QL_MUSIC_DIR in .env" if cfg.in_container()
               else "MUSIC_ROOT")
    return (f"No artist folders in {cfg.MUSIC_ROOT}. If your music is "
            f"somewhere else, set {setting} to that folder.")


def unreadable_artist(name, error) -> str:
    """The one line for an artist folder that couldn't be read."""
    if isinstance(error, OSError) and error.strerror:
        error = error.strerror.lower()
    return (f"Can't read the artist folder {name}: {error}. Check that the "
            "user Qobuz Librarian runs as can read it (PUID and PGID in "
            "Docker).")


def oserr_hint(e: OSError) -> str:
    """Append a PUID/PGID hint when an OSError looks like a NAS perms issue."""
    if e.errno in (errno.EACCES, errno.EROFS):
        return (" - the container user can't write here. On a NAS, set "
                "PUID/PGID to the share owner.")
    if e.errno == errno.ENOSPC:
        return " - disk is full."
    return ""


def auth_lost_msg(exc=None) -> str:
    """Multi-line block for AuthLost catch-and-die sites in CLI modes."""
    if isinstance(exc, DownloadsRefused):
        return f"\n✗  {exc} The run stopped.\n"
    return ("\n✗  Qobuz rejected the token, so the run stopped.\n"
            "   Paste a new token on the Settings page of the web app, or set "
            "QOBUZ_USER_AUTH_TOKEN, then run this again.\n")
