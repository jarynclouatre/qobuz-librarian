"""Error-message and copy helpers shared across CLI/web."""
import errno
import sys

from qobuz_librarian.ui_cli.colors import block

# Exit-code contract for the CLI.
EXIT_GENERAL    = 1   # unspecified failure (default for crashes/unknown)
EXIT_AUTH       = 2   # token invalid / expired
EXIT_LOCK_BUSY  = 3   # another writer holds the lock
EXIT_TRANSIENT  = 4   # network / Qobuz API trouble — retry later
EXIT_CONFIG     = 64  # config missing / unreadable / tool absent


def die(msg: str, code: int = EXIT_GENERAL):
    """Print msg to stderr, wrapped to the terminal, and exit with the code."""
    print(block(msg), file=sys.stderr)
    sys.exit(code)


def plural(n: int, singular: str, plural_form: str | None = None) -> str:
    """Format `n singular` / `n plural` with auto pluralization."""
    word = singular if n == 1 else (plural_form or singular + "s")
    return f"{n} {word}"


def oserr_hint(e: OSError) -> str:
    """Append a PUID/PGID hint when an OSError looks like a NAS perms issue."""
    if e.errno in (errno.EACCES, errno.EROFS):
        return (" — the container user can't write here. On a NAS, set "
                "PUID/PGID to the share owner.")
    if e.errno == errno.ENOSPC:
        return " — disk is full."
    return ""


def auth_lost_msg(context: str = "mid-run") -> str:
    """Multi-line block for AuthLost catch-and-die sites in CLI modes."""
    return (f"\n✗  Auth lost {context} — re-paste your token on Settings "
            "or set QOBUZ_USER_AUTH_TOKEN.\n")
