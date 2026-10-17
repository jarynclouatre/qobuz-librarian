"""Entry point: argument parsing, pre-flight checks, and main dispatch.

Mode runner functions (run_album_mode, run_artist_mode, etc.) are
lazy-imported from qobuz_librarian.modes to keep startup fast.
"""
import argparse
import re
import shlex
import shutil
import subprocess
import sys
import textwrap

from qobuz_librarian import __version__, raise_open_file_limit, run_lock
from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import (
    AuthLost,
    CredentialChanged,
    DownloaderNotReady,
    NoCredsError,
    QobuzAccess,
    QobuzEntitlementError,
    QobuzUnavailable,
)
from qobuz_librarian.integrations.lyrics import _prune_lyric_state_orphans
from qobuz_librarian.integrations.rip import HAVE_MUTAGEN
from qobuz_librarian.library import missing_types
from qobuz_librarian.library.backup import cleanup_old_upgrade_backups
from qobuz_librarian.quality.tiers import streamrip_quality_cap
from qobuz_librarian.queue import journal as queue_state
from qobuz_librarian.queue.persistence import offer_resume_startup_recovery
from qobuz_librarian.ui_cli.colors import (
    C,
    banner,
    block,
    fmt,
    set_color_enabled,
    text_width,
    wrap,
)
from qobuz_librarian.ui_cli.errors import (
    EXIT_AUTH,
    EXIT_CONFIG,
    EXIT_GENERAL,
    EXIT_INTERRUPT,
    EXIT_LOCK_BUSY,
    EXIT_TRANSIENT,
    auth_lost_msg,
    die,
    plural,
)
from qobuz_librarian.ui_cli.logging import attach_file_handler, log, set_quiet, set_verbose, vlog

_STARTUP_RECOVERY_RESULT = None
_RUN_LEASE = None


_QOBUZ_PLAY_RE        = re.compile(r"(?:play|open)\.qobuz\.com/(album|track)/([A-Za-z0-9]+)")
_QOBUZ_STORE_ALBUM_RE = re.compile(r"qobuz\.com/[a-zA-Z-]+/album/[^/]+/([A-Za-z0-9]+)/?(?:[?#]|$)")
_QOBUZ_STORE_TRACK_RE = re.compile(r"qobuz\.com/[a-zA-Z-]+/track/[^/]+/([A-Za-z0-9]+)/?(?:[?#]|$)")


def parse_qobuz_url(url: str) -> tuple[str, str] | None:
    m = _QOBUZ_PLAY_RE.search(url)
    if m:
        return m.group(1), m.group(2)
    m = _QOBUZ_STORE_ALBUM_RE.search(url)
    if m:
        return "album", m.group(1)
    m = _QOBUZ_STORE_TRACK_RE.search(url)
    if m:
        return "track", m.group(1)
    return None


def _recover_startup_queue(authority, *, settle=False):
    """Inspect durable queue state without starting download/import work."""
    from qobuz_librarian.queue.startup_recovery import recover_startup_state

    return recover_startup_state(authority=authority, settle=settle)


def _record_startup_recovery(authority, *, settle=False):
    global _STARTUP_RECOVERY_RESULT
    _STARTUP_RECOVERY_RESULT = _recover_startup_queue(authority, settle=settle)
    return _STARTUP_RECOVERY_RESULT


def _report_settled_downloads(result):
    """Say what start-up did with each download a restart cut off."""
    for settled in getattr(result, "settled", ()):
        label = f"“{settled.label}”"
        if settled.outcome == "requeue" and settled.mode.startswith("web-job:"):
            line = (f"{label} was cut off in the web app before it was "
                    "imported. Its partial download was removed; the web app "
                    "queues it again or offers Retry.")
        elif settled.outcome == "requeue":
            line = (f"{label} was cut off before it was imported. Its partial "
                    "download was removed and it is back in the saved queue.")
        elif settled.outcome == "checked":
            line = f"{label} was filed by Beets before it stopped and is checked again."
        else:
            line = f"{label}: {settled.reason}"
        log.info(fmt(C.YELLOW, wrap(line)))


def _startup_recovery_status():
    return getattr(_STARTUP_RECOVERY_RESULT, "status", None)


def _die_unsettled_startup_recovery(authority, result=None) -> None:
    try:
        authority.close()
    except OSError:
        pass
    if (
        getattr(result, "reason", None) == "post-import-relocation-unsettled"
        and getattr(result, "post_import_relocation", None) is not None
    ):
        from qobuz_librarian.queue.startup_recovery import (
            POST_IMPORT_RELOCATION_LOG_ENTRY,
        )

        relocation = result.post_import_relocation
        # The prose reflows to the terminal; the paths do not. A path is only
        # copyable if every character survives to the screen; reflow would
        # collapse doubled spaces and wrap long ones at inner spaces.
        intro = (
            "\n✗  An interrupted library-folder move could not be verified safely.\n"
            f"   Recovery reason: {relocation.reason or 'reason not reported'}\n"
            "   Paths needing attention:"
            + ("" if relocation.paths else " none reported")
        )
        outro = (
            "   Library changes are paused so the files remain unchanged. See "
            f"the “{POST_IMPORT_RELOCATION_LOG_ENTRY}” entry in the application "
            "log for the same details. Resolve the reported recovery problem, "
            "then restart Qobuz Librarian.\n"
        )
        print(fmt(C.RED, block(intro)), file=sys.stderr)
        for path in relocation.paths:
            print(fmt(C.RED, f"     {path}"), file=sys.stderr)
        print(fmt(C.RED, block(outro)), file=sys.stderr)
        raise SystemExit(EXIT_GENERAL)
    elif getattr(result, "reason", None) == "queue-namespace-blocked":
        paths = getattr(result, "paths", ())
        print(fmt(C.RED, block(
            "\n✗  The saved download queue can't be read.\n"
            "   Nothing was changed and no work was started. The file "
            "involved:" + ("" if paths else " not reported")
        )), file=sys.stderr)
        for path in paths:
            print(fmt(C.RED, f"     {path}"), file=sys.stderr)
        print(fmt(C.RED, block(
            "   Fix its permissions, or if it is damaged, move it out of the "
            "data folder (the downloads it listed will need queuing again), "
            "then run Qobuz Librarian again.\n"
        )), file=sys.stderr)
        raise SystemExit(EXIT_GENERAL)
    elif getattr(result, "reason", None) == "web-completion-unacknowledged":
        message = (
            "\n✗  A download the web app finished is waiting for the web app "
            "to record it.\n"
            "   The album is in your library, and no work was started here. "
            "Let the web app take the library once: start it, or choose "
            "Resume web app in Settings → Mode. Then run this again.\n"
        )
    else:
        from qobuz_librarian.queue.startup_recovery import (
            BLOCKED_DOWNLOAD_LOG_ENTRY,
        )

        # The block reason is an internal diagnostic, so it goes to the log the
        # message names. Nothing else writes it, and the message used to point
        # at a log entry that was never recorded.
        detail = "; ".join(
            f"{entry.item_id}: {entry.reason or 'reason not reported'}"
            for entry in getattr(result, "items", ())
            if entry.phase is queue_state.QueuePhase.BLOCKED
        )
        log.error(
            "%s: %s.",
            BLOCKED_DOWNLOAD_LOG_ENTRY,
            detail or (getattr(result, "reason", "") or "reason not reported"),
        )
        # A state this stop is reached in survives a restart, so the message
        # names where the outstanding work is instead of prescribing one.
        message = (
            "\n✗  An interrupted download could not be cleared safely.\n"
            "   Its staged files were left as they were, and no other work "
            "was started.\n"
            f"   What is still outstanding is under {cfg.STAGING_DIR}. The "
            f"“{BLOCKED_DOWNLOAD_LOG_ENTRY}” entry in the application "
            "log has the technical detail.\n"
        )
    die(fmt(C.RED, message), EXIT_GENERAL)


def _compose_service_name() -> str:
    """Best-effort docker compose service name for diagnostic hints."""
    import os
    h = os.environ.get("HOSTNAME", "").strip()
    if not h or (len(h) == 12 and all(c in "0123456789abcdef" for c in h)):
        return "qobuz-librarian"
    return h


def acquire_run_lock():
    """Acquire the single-writer run lock or exit."""
    global _RUN_LEASE
    try:
        lease = run_lock.acquire("terminal")
    except run_lock.LockBusy as busy:
        if busy.holder == "web":
            message = "\n✗  The web app is using the library.\n"
        elif busy.holder == "terminal":
            message = (f"\n✗  Another terminal run (pid {busy.pid}) is using "
                       "the library.\n")
        else:
            message = (
                f"\n✗  Another Qobuz Librarian process (pid {busy.pid}) holds "
                f"the run lock.\n   Lock file: {cfg.LOCK_FILE}\n"
            )
        message += ("   Only one Qobuz Librarian run can change the library at a "
                    "time.\n\n")
        if busy.holder == "terminal":
            message += "   Run this again once it finishes.\n"
        else:
            message += ("   Either:\n" if busy.holder == "web"
                        else "   If it is the web app, either:\n")
            web_ui = (f"the web app (http://<host>:{cfg.WEB_PUBLIC_PORT})"
                      if _in_container() else "the web app")
            message += (
                f"     1. In {web_ui}, open Settings → Mode and choose\n"
                "        Switch to terminal mode, then run this command again.\n"
                "        Choose Resume web app there when you are done.\n"
            )
            if _in_container():
                _svc = _compose_service_name()
                message += (
                    "     2. Or stop the web container with\n"
                    f"        `docker compose stop {_svc}`, run this command\n"
                    f"        again, then `docker compose start {_svc}`.\n"
                )
            else:
                message += (
                    "     2. Or stop qobuz-librarian-web, run this command again,\n"
                    "        then start it again.\n"
                )
        die(fmt(C.RED, message),
            EXIT_LOCK_BUSY)
    if lease is not None and lease.intact() is True:
        try:
            result = _record_startup_recovery(lease, settle=True)
        except Exception as exc:
            try:
                lease.close()
            except OSError as close_exc:
                vlog(f"run-lock release after recovery failure: {close_exc}")
            vlog(f"startup recovery check failed: {exc}")
            die(fmt(
                C.RED,
                "\n✗  The saved recovery state could not be checked safely.\n"
                "   The run lock was released and no library work was "
                "started. Check the data-folder permissions, then try "
                "again.\n",
            ), EXIT_GENERAL)
        except BaseException:
            try:
                lease.close()
            except OSError:
                pass
            raise
        from qobuz_librarian.queue.startup_recovery import StartupRecoveryStatus
        _report_settled_downloads(result)
        if result.status is StartupRecoveryStatus.ATTENTION_REQUIRED:
            _die_unsettled_startup_recovery(lease, result)
        _RUN_LEASE = lease
        return lease
    if lease is not None:
        lease.close()
    die(fmt(
        C.RED,
        "\n✗  The run lock could not be taken.\n"
        f"   Lock file: {cfg.LOCK_FILE}\n\n"
        "   Nothing was changed. Check the data-volume permissions and "
        "filesystem locking support, then try again.\n",
    ), EXIT_GENERAL)


def _in_container() -> bool:
    return cfg.in_container()


def _missing_tool_hint(tool: str, install_hint: str) -> str:
    if _in_container():
        return (f"\n✗  `{tool}` not in PATH inside the container.\n"
                f"   This means the image is broken. Rebuild with "
                f"`docker compose build --no-cache`.\n")
    return f"\n✗  `{tool}` not in PATH. {install_hint}\n"


def check_rip():
    try:
        r = subprocess.run(["rip", "--version"], capture_output=True, text=True, timeout=10,
                           env=cfg.child_env())
        if r.returncode != 0:
            # rip is on PATH (no FileNotFoundError) but exited nonzero; it is
            # installed and broken, not missing.
            # streamrip colours its errors; foreign escapes mid-message
            # would make block() give up wrapping the whole thing.
            detail = re.sub(r"\x1b\[[0-9;]*m",
                            "", (r.stderr or r.stdout or "")).strip()
            die(fmt(C.RED,
                f"\n✗  `rip --version` exited {r.returncode}. streamrip is "
                f"installed but not working"
                + (f":\n     {detail}" if detail else ".")
                + "\n   Reinstall it with `pipx reinstall streamrip` "
                "(https://github.com/nathom/streamrip).\n"),
                EXIT_CONFIG)
    except FileNotFoundError:
        die(fmt(C.RED, _missing_tool_hint(
            "rip", "Install streamrip first: `pipx install streamrip` "
            "(https://github.com/nathom/streamrip).")), EXIT_CONFIG)
    except (OSError, subprocess.TimeoutExpired) as e:
        die(fmt(C.RED,
            f"\n✗  `rip --version` couldn't run ({e}). The streamrip binary "
            "may be broken. Reinstall it with `pipx reinstall streamrip`.\n"),
            EXIT_CONFIG)


def check_media_tools():
    # flac verifies every downloaded track and metaflac reads its bit depth
    # for the resample; ffmpeg does the hi-res downsample.
    for tool, hint in (
        ("flac", "Install the FLAC tools via your package manager "
                 "(e.g. `apt install flac`, `brew install flac`)."),
        ("ffmpeg", "Install ffmpeg via your package manager "
                   "(e.g. `apt install ffmpeg`, `brew install ffmpeg`)."),
    ):
        if shutil.which(tool) is None:
            die(fmt(C.RED, _missing_tool_hint(tool, hint)), EXIT_CONFIG)


def check_beets():
    # The import runs after the whole album is downloaded, so a missing Beets
    # would leave it stranded in staging.
    from qobuz_librarian.integrations import beets

    problem = beets.beets_runtime_problem()
    if problem is None:
        return
    fix = (
        "Check that the Beets config volume is mounted writably, or rebuild "
        "the image with `docker compose build --no-cache`."
        if _in_container() else
        "Install beets 2.14.1 and give it a config, or run with --no-import."
    )
    die(fmt(C.RED, block(
        f"\n✗  Beets cannot import downloads: {problem}\n   {fix}\n")),
        EXIT_CONFIG)


def require_music_root():
    from qobuz_librarian.library import collection_snapshot

    state, recorded = collection_snapshot.music_root_write_state()
    if state == "ready":
        return
    if state == "recorded_empty":
        albums = "album" if recorded == 1 else "albums"
        problem = (
            f"\n✗  The music folder has no artist folders, but the last "
            f"collection backup recorded {recorded:,} {albums}.\n"
        )
        fix = (
            "   Check that the music folder is mounted. If those albums really "
            "are gone, open web Settings → Collection backup, choose Back up "
            "now, then Replace anyway before retrying.\n"
        )
    elif state == "backup_unreadable":
        problem = (
            "\n✗  The music folder has no artist folders, and the last "
            "collection backup could not be read safely.\n"
        )
        fix = (
            "   Check that the music folder is mounted and that the collection-"
            "backup folder is readable. Refusing to proceed.\n"
        )
    elif _in_container():
        problem = "\n✗  The /music mount is missing or can't be read.\n"
        fix = ("   Set QL_MUSIC_DIR in .env to the host folder that holds your "
               "artist folders, check that it exists, then run this again.\n")
    else:
        problem = (f"\n✗  The music folder {cfg.MUSIC_ROOT} is missing or "
                   "can't be read.\n")
        fix = ("   Set MUSIC_ROOT to the folder that holds your artist "
               "folders, then run this again.\n")
    die(fmt(C.RED, problem + fix), EXIT_CONFIG)


class _ExitOneArgParser(argparse.ArgumentParser):
    """ArgumentParser that exits 1 (EXIT_GENERAL) on parse errors instead
    of argparse's default 2."""

    def exit(self, status=0, message=None):
        if status == 2:
            status = EXIT_GENERAL
        return super().exit(status, message)


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, width=text_width())

    def _split_lines(self, text, width):
        # Flag names in the help text stay whole rather than breaking at
        # their hyphens.
        text = self._whitespace_matcher.sub(" ", text).strip()
        return textwrap.wrap(text, width, break_on_hyphens=False)

    def _format_action_invocation(self, action):
        if isinstance(action, argparse.BooleanOptionalAction):
            option = action.option_strings[0]
            return option.replace("--", "--[no-]", 1)
        return super()._format_action_invocation(action)


_HELP_EXAMPLES = (
    ("qobuz-librarian", "interactive menu"),
    ("qobuz-librarian https://open.qobuz.com/album/abc", "one album by URL"),
    ('qobuz-librarian "radiohead in rainbows"', "search and download"),
    ('qobuz-librarian --artist "Paysage d\'Hiver"', "fill artist gaps"),
    ("qobuz-librarian --upgrade-walk --auto-safe",
     "unattended upgrade pass"),
    ("qobuz-librarian --dry-run --artist Beatles",
     "preview without downloading"),
)


def _wrap_shell_command(command, width, *, initial_indent="  ",
                        continuation_indent="    "):
    tokens = re.findall(r"(?:'[^']*'|\"[^\"]*\"|[^\s'\"]+)+", command)
    command_lines = []
    line = initial_indent
    for token in tokens:
        separator = "" if line.isspace() else " "
        if not line.isspace() and len(line + separator + token) > width - 2:
            command_lines.append(line + " \\")
            line = continuation_indent + token
        else:
            line += separator + token
    command_lines.append(line)
    return command_lines


def _help_example(command, comment, width):
    command_lines = _wrap_shell_command(command, width)

    suffix = "  # " + comment
    if len(command_lines[-1] + suffix) <= width:
        command_lines[-1] += suffix
        return command_lines
    command_lines.extend(textwrap.wrap(
        comment,
        width=width,
        initial_indent="    # ",
        subsequent_indent="      ",
        break_long_words=False,
        break_on_hyphens=False,
    ))
    return command_lines


def _help_description():
    return textwrap.fill(
        "Qobuz Librarian: download albums and artists from Qobuz and keep a "
        "library complete, fetching only what is missing. Run with no "
        "arguments for an interactive menu.",
        width=text_width(),
        break_long_words=False,
        break_on_hyphens=False,
    )


def _beets_recovery_command(album_dirs=()):
    """The manual import for staged albums: the given folders, or every
    download run folder."""
    if album_dirs:
        targets = " ".join(shlex.quote(str(path)) for path in album_dirs)
    else:
        # Beets skips names starting with a dot, so a run folder has to be
        # named itself; importing the staging folder finds nothing.
        targets = f"{shlex.quote(str(cfg.STAGING_DIR))}/.qobuz-run-*"
    command = f"beet import {targets}"
    if _in_container():
        service = shlex.quote(_compose_service_name())
        if not album_dirs:
            command = f"sh -c {shlex.quote(command)}"
        return f"docker compose run --rm {service} {command}"
    if cfg.BEETS_CONFIG_DIR != cfg.HOME / ".config" / "beets":
        command = f"BEETSDIR={shlex.quote(str(cfg.BEETS_CONFIG_DIR))} {command}"
    return command


def _help_epilog():
    width = text_width()
    lines = ["Examples (credentials required):"]
    for command, comment in _HELP_EXAMPLES:
        lines.extend(_help_example(command, comment, width))

    recovery_heading = (
        "After --no-import (Compose host):"
        if _in_container() else "After --no-import:")
    lines.extend(("", recovery_heading))
    lines.extend(_help_example(
        _beets_recovery_command(), "finish importing staged albums", width))

    lines.extend(("", *textwrap.wrap(
        "Credentials: set them on the web UI Settings page first, or use "
        "QOBUZ_USER_AUTH_TOKEN and QOBUZ_USER_ID environment variables.",
        width=width,
        break_long_words=False,
        break_on_hyphens=False,
    )))
    lines.extend(textwrap.wrap(
        f"On a fresh install, open http://<host>:{cfg.WEB_PUBLIC_PORT}/settings.",
        width=width,
        break_long_words=False,
        break_on_hyphens=False,
    ))
    lines.extend(("", *textwrap.wrap(
        "Only in the web app: the Library scan and its reviews, New releases "
        "checks, Discover, dismissing and bringing back albums, putting "
        "downsampled originals back, restoring from a collection backup, and "
        "the Settings fields --settings leaves out.",
        width=width,
        break_long_words=False,
        break_on_hyphens=False,
    )))
    lines.extend(("", "Exit codes:"))
    for code, description in (
        ("0", "success"),
        ("1", "general failure"),
        ("2", "authentication token invalid or missing"),
        ("3", "another run is using the library"),
        ("4", "transient network or API error; retry later"),
        ("64", "configuration or required tool missing"),
        ("130", "interrupted with Ctrl-C"),
    ):
        prefix = f"  {code:<4}"
        lines.extend(textwrap.wrap(
            description,
            width=width,
            initial_indent=prefix,
            subsequent_indent=" " * len(prefix),
            break_long_words=False,
            break_on_hyphens=False,
        ))
    return "\n".join(lines)


def parse_args():
    p = _ExitOneArgParser(
        usage="%(prog)s [options] [query ...]",
        add_help=False,
        description=_help_description(),
        formatter_class=_HelpFormatter,
        epilog=_help_epilog())
    # Grouped so --help reads as sections instead of one wall. Grouping only:
    # every flag is still spelled and parsed exactly as before, so nothing
    # already written down stops working.
    modes = p.add_argument_group(
        "What to run", "Pick one. With none of these, the interactive menu opens.")
    offers = p.add_argument_group(
        "What gets offered", "Which releases the catalogue steps suggest.")
    getting = p.add_argument_group(
        "Downloading and importing", "How a run behaves once it has picks.")
    lyrics = p.add_argument_group("Lyrics", "Only with --lyrics-walk.")
    migration = p.add_argument_group("Migration", "Only with --migrate.")
    output = p.add_argument_group("Output and help")

    modes.add_argument("query", nargs="*", help="search query or Qobuz album URL")
    modes.add_argument("--artist",       metavar="NAME",
                   help="one artist: fill gaps, then offer albums you're "
                        "missing (same as menu Artist)")
    modes.add_argument("--library-walk", action="store_true",
                   help="walk every artist: fill gaps, then offer albums you're "
                        "missing; queue as you go (same as menu Library walk).")
    modes.add_argument("--gap-fill", "--album-gaps", dest="album_gaps",
                   action="store_true",
                   help="fill missing tracks in every incomplete album you own; "
                        "never suggests albums you don't have (same as menu "
                        "Gap Fill).")
    modes.add_argument("--repair", action="store_true",
                   help="re-download damaged (truncated or corrupt) tracks "
                        "you own. '*' at the artist prompt sweeps the whole "
                        "library.")
    modes.add_argument("--upgrade-walk", action="store_true",
                   help="review quality upgrades: the Library scan's saved "
                        "results when current, otherwise a fresh check. "
                        "Asks per artist; Enter skips.")
    modes.add_argument("--downsample-walk", action="store_true",
                   help="scan the library for files above 48 kHz and downsample "
                        "them to 44.1/48 kHz in place (per-artist confirm; "
                        "--dry-run lists only). Local; no Qobuz login needed.")
    modes.add_argument("--lyrics-walk", action="store_true",
                   help="fetch lyrics for tracks already in the library that "
                        "are missing them (LYRICS_FORMAT / LYRICS_PROVIDERS "
                        "settings apply). Local; no Qobuz login needed.")
    modes.add_argument("--migrate", action="store_true",
                   help="one-time setup: reorganise an existing library into the "
                        "Artist/Album layout (same as menu Library migration; "
                        "local-only; no Qobuz login needed)")
    modes.add_argument("--settings", action="store_true",
                   help="set download quality, whether downsampling keeps "
                        "the originals, and behaviour toggles from the "
                        "terminal (same store the web Settings page saves to)")
    modes.add_argument("--reset-walk-seen", action="store_true",
                   help="forget which artists and albums the walks have "
                        "already checked, and exit")

    offers.add_argument("--no-catalog",   action="store_true",
                   help="skip missing album suggestions in Artist mode and "
                        "Library walk")
    # Missing albums are the releases Qobuz files as the artist's albums.
    # These add its other lists for one run; left unset, each follows its
    # switch on the Library review.
    offers.add_argument("--include-live", action=argparse.BooleanOptionalAction,
                   default=None,
                   help="list live releases among missing albums in Artist "
                        "mode and Library walk (default: the Library "
                        "review's Live switch)")
    offers.add_argument("--include-comps", action=argparse.BooleanOptionalAction,
                   default=None,
                   help="list compilations among missing albums in Artist "
                        "mode and Library walk (default: the Library "
                        "review's Compilations switch)")
    offers.add_argument("--include-singles", action=argparse.BooleanOptionalAction,
                   default=None,
                   help="list EPs and singles among missing albums in "
                        "Artist mode and Library walk (default: the Library "
                        "review's EPs & singles switch)")
    offers.add_argument("--include-bonus", action=argparse.BooleanOptionalAction,
                   default=None,
                   help="offer to fill an album whose only missing tracks are "
                        "other versions of songs it has (remixes, live takes, "
                        "demos) in Artist mode, Library walk and Gap fill "
                        "(default: the Library review's Bonus versions switch)")
    # Default comes from config (PREFER_HIRES, env/Settings overridable) so
    # CLI and web behave the same. --no-prefer-hires overrides per-run.
    offers.add_argument("--prefer-hires", dest="prefer_hires",
                   action=argparse.BooleanOptionalAction,
                   default=cfg.PREFER_HIRES,
                   help="sort 24-bit / higher sample rate results first")

    getting.add_argument("--dry-run",      action="store_true",
                   help="show the plan and change nothing; a download a "
                        "restart cut off is still settled first, as at every "
                        "start")
    getting.add_argument("--force",        action="store_true",
                   help="download every track of the album again (search or "
                        "album URL only)")
    getting.add_argument("--yes",          action="store_true",
                   help="auto-confirm the confirmations; a gap fill, an "
                        "upgrade, a downsample, a repair or an in-place "
                        "migration can replace or move files you already have. "
                        "Choices still ask (which artist to walk, which "
                        "release to take, consolidating sibling folders, "
                        "whether a first downsample keeps the originals), and "
                        "so do the backup and collision questions of --force, "
                        "discarding a saved queue, and migrating to a disk "
                        "short on space")
    # Unattended upgrade-walk gate.
    getting.add_argument("--auto-safe",    action="store_true",
                   help="auto-confirm only the safe upgrades (requires --upgrade-walk).")
    getting.add_argument("--no-import",    action="store_true",
                   help="download but skip beets import; see the recovery "
                        "command below")
    # Passive auto-upgrade is OFF unless AUTO_UPGRADE_ENABLED is set or the
    # explicit Upgrade walk is run.
    getting.add_argument("--no-upgrade",   action="store_true",
                   help="turn off quality upgrades for this run")
    getting.add_argument("--no-downsample",  dest="no_downsample",
                   action="store_true",
                   help="skip downsampling new downloads for this run (when "
                        "Downsample new hi-res downloads is on)")
    # Default from config (CONSOLIDATE, env/CLI overridable).
    getting.add_argument("--consolidate",  dest="consolidate",
                   action=argparse.BooleanOptionalAction,
                   default=cfg.CONSOLIDATE,
                   help="after import, scan sibling folders and offer to consolidate")
    getting.add_argument("--migrate-multi-artist", dest="migrate_multi_artist",
                   action=argparse.BooleanOptionalAction,
                   default=cfg.MIGRATE_MULTI_ARTIST,
                   help="after import, safely re-file 'Primary, Other' albums "
                        "under 'Primary'")

    lyrics.add_argument("--lyrics-rescan", action="store_true",
                   help="also look up tracks checked in the last 30 days.")
    lyrics.add_argument("--lyrics-synced-only", action="store_true",
                   help="only write timed (synced) lyrics, never plain.")

    migration.add_argument("--migrate-src", dest="migrate_src", metavar="PATH", default="",
                   help="migration source library to read (overrides MIGRATE_SRC)")
    migration.add_argument("--migrate-dest", dest="migrate_dest", metavar="PATH", default="",
                   help="where migration builds the organised copy "
                        "(overrides MIGRATE_DEST)")
    migration.add_argument("--in-place", dest="in_place", action="store_true",
                   help="MOVE files into place instead of copying "
                        "(default copies, leaving originals untouched)")
    migration.add_argument("--acoustid", dest="acoustid", action="store_true",
                   help="fingerprint files whose tags can't place them "
                        "(slower, needs network; no key required)")

    output.add_argument("--verbose",      action="store_true", help="show detection details")
    output.add_argument("--quiet", "-q",  action="store_true",
                   help="suppress info-level console output (warnings/errors "
                        "still print; the log file keeps recording)")
    output.add_argument("--no-color",     action="store_true", help="disable ANSI colours")
    output.add_argument("--version", action="version", version=f"qobuz-librarian {__version__}")
    output.add_argument("-h", "--help", action="help",
                   help="show this help and exit")
    args = p.parse_args()
    # Per-run override of cfg.AUTO_UPGRADE_ENABLED.
    args.auto_upgrade = cfg.AUTO_UPGRADE_ENABLED
    # The walk / migrate / reset modes are whole-library or local-only runs that
    # read none of the album- or artist-scan flags below, so naming one of those
    # flags alongside them would silently drop it.
    other_run_mode = (args.upgrade_walk or args.downsample_walk
                      or args.lyrics_walk or args.migrate or args.reset_walk_seen
                      or args.library_walk
                      or args.album_gaps or args.repair or args.settings)
    # Reject flag/mode combinations that would otherwise be silently dropped
    # or accepted.
    if args.force and not args.query:
        p.error("--force only applies to a search or an album URL, not "
                "--artist or a walk or migrate mode")
    # --migrate's extra options mean nothing without it.
    if (args.in_place or args.acoustid or args.migrate_src or args.migrate_dest) \
            and not args.migrate:
        p.error("--in-place / --acoustid / --migrate-src / --migrate-dest only "
                "apply with --migrate")
    # The --include-* flags only affect the missing-albums step of artist
    # mode, which the library walk runs per artist, so they apply there too.
    for flag in ("include_live", "include_comps", "include_singles"):
        if (getattr(args, flag) and not (args.artist or args.library_walk)
                and (args.query or other_run_mode)):
            p.error(f"--{flag.replace('_', '-')} only applies to artist mode "
                    "and --library-walk")
    # --include-bonus only affects the gap-fill step those modes share.
    if (args.include_bonus and not (args.artist or args.library_walk
                                    or args.album_gaps)
            and (args.query or other_run_mode)):
        p.error("--include-bonus only applies to artist mode, --library-walk "
                "and --gap-fill")
    # --no-catalog skips that same missing-albums step.
    if (args.no_catalog and not (args.artist or args.library_walk)
            and (args.query or other_run_mode)):
        p.error("--no-catalog only applies to artist mode and --library-walk")
    if args.upgrade_walk and args.query:
        p.error("--upgrade-walk covers the whole library. Drop the "
                "query, or run a normal search without --upgrade-walk")
    # --artist dispatches before the positional query, so extra words after the
    # artist name would be silently dropped. Reject so the user picks one.
    if args.artist and args.query:
        p.error("--artist NAME scans that one artist. Drop the extra words, "
                "or search them as an album without --artist")
    # Exactly one whole-run mode executes per invocation; main() dispatches
    # the first it finds and returns, so a second would be silently dropped.
    requested = [name for name, on in (
        ("a search or album URL", bool(args.query)),
        ("--artist", args.artist is not None),
        ("--library-walk", args.library_walk),
        ("--gap-fill", args.album_gaps),
        ("--repair", args.repair),
        ("--upgrade-walk", args.upgrade_walk),
        ("--downsample-walk", args.downsample_walk),
        ("--lyrics-walk", args.lyrics_walk),
        ("--migrate", args.migrate),
        ("--reset-walk-seen", args.reset_walk_seen),
        ("--settings", args.settings),
    ) if on]
    if len(requested) > 1:
        p.error("run one mode at a time; got " + ", ".join(requested))
    if args.settings and args.quiet:
        p.error("--quiet would hide the choices --settings asks about; drop --quiet")
    # With no mode, the run falls through to the interactive menu, whose
    # prompts --quiet would silence, leaving a bare input cursor.
    if args.quiet and not requested:
        p.error("--quiet is for unattended runs and silences the interactive "
                "menu. Name a mode (a query, --artist, --upgrade-walk, …) "
                "or drop --quiet")
    # --no-upgrade with --upgrade-walk is contradictory.
    if args.no_upgrade and args.upgrade_walk:
        p.error("--no-upgrade conflicts with --upgrade-walk")
    # --auto-safe only gates the unattended upgrade walk.
    if args.auto_safe and not args.upgrade_walk:
        p.error("--auto-safe only applies to --upgrade-walk")
    # An empty --artist (e.g. `--artist "$VAR"` with VAR unset) is falsy, so it
    # would silently fall through to the interactive menu instead of running
    # artist mode, which would be confusing in a script. Reject it.
    if args.artist is not None and not args.artist.strip():
        p.error("--artist needs an artist name")
    if (args.lyrics_rescan or args.lyrics_synced_only) and not args.lyrics_walk:
        p.error("--lyrics-rescan / --lyrics-synced-only only apply with "
                "--lyrics-walk")
    switched_on = missing_types.load()
    for flag, release_type in (("include_live", missing_types.LIVE),
                               ("include_comps", missing_types.COMPILATION),
                               ("include_singles", missing_types.EP)):
        if getattr(args, flag) is None:
            setattr(args, flag, release_type in switched_on)
    if args.include_bonus is None:
        args.include_bonus = missing_types.BONUS in switched_on
    return args


def main():
    raise_open_file_limit()
    # Apply the web Settings page's persisted overrides before parse_args
    # reads cfg.* into the default flags.
    try:
        from qobuz_librarian.web import settings_store
        settings_store.load()
    except (OSError, ValueError) as exc:
        die(fmt(
            C.RED,
            f"\n✗  Couldn't read the saved settings: {exc}\n"
            "   Refusing to run with environment defaults because that could "
            "change download or file-handling behaviour. Fix the data-folder "
            "permissions or recover the settings file, then try again.\n",
        ), EXIT_CONFIG)

    args = parse_args()
    set_verbose(args.verbose)
    set_quiet(args.quiet)
    try:
        cfg.validate_storage_roots()
    except ValueError as exc:
        die(fmt(
            C.RED,
            f"\n✗  Invalid storage paths: {exc}\n"
            "   Refusing to proceed.\n",
        ), EXIT_CONFIG)
    # role="cli" → a separate log file so a `docker exec` CLI run sharing the
    # container with the long-lived web server can't race it on log rollover.
    attach_file_handler(cfg.APP_LOG_FILE, cfg.LOG_LEVEL, role="cli")
    if args.quiet:
        set_color_enabled(False)

    if args.no_color:
        set_color_enabled(False)

    if args.reset_walk_seen and args.dry_run:
        present = [str(f) for f in (cfg.WALK_SEEN_FILE, cfg.ALBUM_WALK_SEEN_FILE)
                   if f.exists()]
        if present:
            log.info(fmt(C.GRAY, "  --dry-run: would clear the lists of artists and albums already checked:"))
            for r in present:
                log.info(fmt(C.GRAY, f"     {r}"))
        else:
            log.info(fmt(C.GRAY, "  No list of checked artists or albums to clear."))
        return

    banner("Qobuz Librarian: search · artist · library · repair · upgrade")

    # Single-instance lock first; fail fast before doing any other work.
    # Hold the file handle for the lifetime of main() so the lock persists.
    _lockfile = acquire_run_lock()  # noqa: F841

    from qobuz_librarian.queue.startup_recovery import StartupRecoveryStatus
    saved_queue = (
        _startup_recovery_status() is StartupRecoveryStatus.RESUME_REQUIRED)
    queue_started = saved_queue and any(
        item.phase is not queue_state.QueuePhase.PENDING
        for item in _STARTUP_RECOVERY_RESULT.items
    )
    # These download through the one saved queue, so a kept queue that
    # nothing has started from holds up only them.
    queue_modes = (
        args.artist,
        args.library_walk,
        args.album_gaps,
        args.repair,
        args.query,
    )
    other_modes = (
        args.reset_walk_seen,
        args.migrate,
        args.lyrics_walk,
        args.downsample_walk,
        args.upgrade_walk,
    )
    def queue_refusal(uses_queue, other_work):
        if queue_started and (uses_queue or other_work):
            return fmt(
                C.YELLOW,
                "\n⚠  An interrupted download must be resumed before other work.\n"
                "   Run Qobuz Librarian without a mode or search argument and "
                "choose Resume when it offers the saved queue.\n",
            )
        if saved_queue and uses_queue:
            return fmt(
                C.YELLOW,
                "\n⚠  A saved download queue must be resumed or discarded before "
                "another download.\n"
                f"   It is in {cfg.QUEUE_JOURNAL_DIR}. Run Qobuz Librarian without "
                "a mode or search argument to choose.\n",
            )
        return None

    refusal = queue_refusal(any(queue_modes), any(other_modes))
    if refusal:
        die(refusal, EXIT_GENERAL)
    resume_saved_queue = saved_queue and not any(queue_modes + other_modes)

    if args.reset_walk_seen:
        removed = []
        for f in (cfg.WALK_SEEN_FILE, cfg.ALBUM_WALK_SEEN_FILE):
            try:
                f.unlink()
                removed.append(str(f))
            except FileNotFoundError:
                pass
            except OSError as e:
                die(fmt(C.RED,
                    f"\n✗  Couldn't delete {f}: {e}\n"
                    "   Check the volume permissions / PUID-PGID; /data must be writable.\n"),
                    EXIT_GENERAL)
        if removed:
            log.info(fmt(C.GREEN, "  ✓  Cleared the lists of artists and albums already checked:"))
            for r in removed:
                log.info(fmt(C.GRAY, f"     {r}"))
        else:
            log.info(fmt(C.GRAY, "  No list of checked artists or albums to clear."))
        return

    # Local-only: reads/writes the same settings store the web Settings page
    # and the downsample walk's keep-originals prompt use.
    if args.settings:
        from qobuz_librarian.modes.settings_cli import run_settings_mode
        raise SystemExit(run_settings_mode(args))

    # Keep the interactive menu and saved-work choices available on a local-
    # only box.
    download_stack = {}

    def prepare_download_stack():
        if download_stack.get("tools_ready"):
            return
        check_rip()
        check_media_tools()
        if not args.no_import and not args.dry_run:
            check_beets()
        from qobuz_librarian.api.auth import (
            check_env_credentials,
            sweep_streamrip_run_copies,
        )
        sweep_streamrip_run_copies()
        check_env_credentials()
        cap_depth, cap_rate = streamrip_quality_cap()
        vlog(
            f"streamrip quality cap: {cap_depth}-bit/"
            f"{cap_rate/1000:g}kHz"
        )
        download_stack["tools_ready"] = True

    def remote_token(access):
        try:
            from qobuz_librarian.api.client import authorize_qobuz_action

            credentials = authorize_qobuz_action(access)
        except NoCredsError:
            die(fmt(C.RED,
                "\n✗  No Qobuz credentials configured.\n"
                "   Paste your token and your email or user ID on the Settings page "
                f"(http://<host>:{cfg.WEB_PUBLIC_PORT}/settings)\n"
                "   or set QOBUZ_USER_AUTH_TOKEN and QOBUZ_USER_ID in your environment.\n"),
                EXIT_AUTH)
        user_id, token = credentials.user_id, credentials.token
        require_music_root()
        if not HAVE_MUTAGEN:
            log.info(fmt(C.YELLOW,
                "  ⚠  mutagen not installed; falling back to filename-only "
                "detection."))
            if _in_container():
                log.info(fmt(C.GRAY,
                    "     The bundled image installs mutagen by default. If "
                    "it's missing here, rebuild with `docker compose build "
                    "--no-cache`."))
            else:
                log.info(fmt(C.GRAY,
                    "     Install: `pip install mutagen` (or via pipx)."))
        from qobuz_librarian.api.client import bind_download_preflight
        token = bind_download_preflight(token, prepare_download_stack)
        if access is QobuzAccess.DOWNLOAD_ACTION:
            prepare_download_stack()
        vlog(f"user_id: {user_id}  •  music root: {cfg.MUSIC_ROOT}")
        if args.verbose:
            if _in_container():
                log.info(fmt(C.GRAY,
                    f"  compose:    {cfg.COMPOSE_FILE}  "
                    "(host-side; not visible from container)"))
            else:
                log.info(fmt(C.GRAY,
                    f"  compose:    {cfg.COMPOSE_FILE}  "
                    f"({'present' if cfg.COMPOSE_FILE.exists() else 'MISSING'})"))
            log.info(fmt(C.GRAY, f"  staging:    {cfg.STAGING_DIR}"))
            log.info(fmt(C.GRAY, f"  lock:       {cfg.LOCK_FILE}"))
        return token

    def catalogue_token():
        return remote_token(QobuzAccess.CATALOGUE_ACTION)

    def download_token():
        return remote_token(QobuzAccess.DOWNLOAD_ACTION)

    def offer_saved_queue():
        """Offer the saved queue: resume, keep or discard it. None when the
        prompt was abandoned."""
        nonlocal saved_queue, queue_started
        try:
            choice = offer_resume_startup_recovery(
                args,
                download_token,
                _STARTUP_RECOVERY_RESULT,
            )
        except (AuthLost, QobuzUnavailable):
            raise
        except Exception as exc:
            die(fmt(
                C.RED,
                "\n✗  The interrupted queue could not be resumed safely.\n"
                f"   {exc}\n"
                "   Its saved recovery state was left unchanged.\n",
            ), EXIT_GENERAL)
        result = _record_startup_recovery(_lockfile)
        if result.status is StartupRecoveryStatus.ATTENTION_REQUIRED:
            _die_unsettled_startup_recovery(_lockfile, result)
        saved_queue = result.status is StartupRecoveryStatus.RESUME_REQUIRED
        queue_started = saved_queue and any(
            item.phase is not queue_state.QueuePhase.PENDING
            for item in result.items
        )
        if saved_queue:
            if queue_started:
                log.info(fmt(
                    C.YELLOW,
                    "  Interrupted queue kept. Other work remains paused until "
                    "it is resumed.",
                ))
            else:
                log.info(fmt(C.YELLOW, wrap(
                    f"Queue kept in {cfg.QUEUE_JOURNAL_DIR}. Other downloads "
                    "wait until it is resumed or discarded.")))
        return choice

    # An album put back from a copy has new inodes; its saved rows are
    # sealed again so they are not refused for them.
    from qobuz_librarian import saved_views
    from qobuz_librarian.library import backup as backup_mod
    if backup_mod.on_restored is None:
        backup_mod.on_restored = saved_views.reseal_put_back_album

    def startup_housekeeping():
        from qobuz_librarian import beets_db
        from qobuz_librarian.integrations import beets as beets_mod
        from qobuz_librarian.integrations import staging as staging_mod
        from qobuz_librarian.modes import process as process_mode
        try:
            beets_db.remove_old_copies()
            beets_mod.remove_stale_import_overrides()
            process_mode.finish_interrupted_upgrades()
            if not saved_queue:
                staging_mod.remove_empty_retry_groups()
        except Exception as e:
            vlog(f"interrupted upgrade settle error: {e}")
        try:
            backup_mod.settle_interrupted_restores()
            for path, origin in backup_mod.albums_left_in_backups():
                backup = backup_mod.album_backup_to_put_back(path, origin)
                if backup is not None and backup_mod.restore_upgrade_backup(
                        backup, origin, replace_partial=False):
                    log.info(fmt(C.YELLOW,
                        f"  ⟲  Put back {origin}, which an interrupted "
                        "upgrade or repair had moved aside."))
            for origin in backup_mod.put_back_interrupted_backups():
                log.info(fmt(C.YELLOW,
                    f"  ⟲  Put back {origin}, which an interrupted "
                    "upgrade or repair had moved aside."))
            left = backup_mod.albums_left_in_backups()
            if left:
                log.info(fmt(C.YELLOW, wrap(
                    f"{plural(len(left), 'album')} "
                    f"{'is' if len(left) == 1 else 'are'} in the backup "
                    "folder and missing from the library: "
                    + "; ".join(str(origin) for _path, origin in left)
                    + f". Restore {'it' if len(left) == 1 else 'them'} in "
                    "Settings > Diagnostics in the web app.",
                    indent="  ", hanging="  ")))
        except Exception as e:
            vlog(f"album put-back error: {e}")
        # Sweep upgrade-backup dir of anything older than retention window.
        # Cheap (just stat + rmtree on stale dirs); silent unless something happens.
        try:
            n_swept = cleanup_old_upgrade_backups()
            if n_swept:
                log.info(fmt(C.GRAY,
                    f"  ⟳  Cleaned up {plural(n_swept, 'old upgrade backup')} "
                    f"(>{cfg.UPGRADE_BACKUP_RETENTION_DAYS} days)."))
        except Exception as e:
            # Don't let backup-housekeeping fail the run.
            vlog(f"upgrade-backup cleanup error: {e}")
        # Prune orphan staging-path entries from lyric_fetch's
        # state file (created during pre-import lyric runs).
        try:
            _prune_lyric_state_orphans()
        except Exception as e:
            vlog(f"lyric-state prune error: {e}")
        # Drop tag-cache rows whose file has since been moved or deleted.
        try:
            from qobuz_librarian.library import flac_cache
            flac_cache.prune_missing()
        except Exception as e:
            vlog(f"flac-cache prune error: {e}")
        # Drop repair-cache ISRC lookups that have aged past the TTL.
        try:
            from qobuz_librarian.library import repair_cache
            repair_cache.prune_expired()
        except Exception as e:
            vlog(f"repair-cache prune error: {e}")

    # Housekeeping deletes expired backups and rewrites state files, so a
    # dry run skips it. A queue that stopped part way holds all other work
    # until it is resumed, so it waits for that; one not yet started is
    # resumed onto a settled library.
    housekept = not args.dry_run and not queue_started
    if housekept:
        startup_housekeeping()
    if resume_saved_queue:
        recovery_choice = offer_saved_queue()
        if recovery_choice is None:
            raise SystemExit(EXIT_GENERAL)

    if not args.dry_run and not queue_started and not housekept:
        startup_housekeeping()

    # Library migration is local-only: it reorganises files on disk and never
    # touches Qobuz.
    if args.migrate:
        from qobuz_librarian.modes.migrate import run_migrate_mode
        raise SystemExit(run_migrate_mode(args))

    # Lyrics backfill reads/writes library files and fetches from lyric
    # providers; no streamrip, ffmpeg or Qobuz token involved.
    if args.lyrics_walk:
        require_music_root()
        from qobuz_librarian.modes.lyrics import run_library_lyrics_mode
        raise SystemExit(run_library_lyrics_mode(args))

    # Downsample walk is local-only; it reads hi-res files off disk and
    # resamples them in place, never touching Qobuz.
    if args.downsample_walk:
        check_media_tools()
        require_music_root()
        from qobuz_librarian.modes.downsample import run_downsample_walk_mode
        raise SystemExit(run_downsample_walk_mode(args))

    # Single-shot flag paths first (each skips the menu loop), then positional
    # args / URL → album mode, then the interactive menu. The single-shot paths
    # still respect AuthLost / KeyboardInterrupt cleanly; all caught at the
    # bottom by main()'s wrapper.
    if args.artist:
        from qobuz_librarian.modes.artist import run_artist_mode
        raise SystemExit(run_artist_mode(args.artist, args, catalogue_token()))

    if args.library_walk:
        from qobuz_librarian.modes.walk import run_walk_queued_mode
        raise SystemExit(run_walk_queued_mode(args, catalogue_token()))

    if args.album_gaps:
        from qobuz_librarian.modes.walk import run_album_walk_mode
        raise SystemExit(run_album_walk_mode(args, catalogue_token()))

    if args.repair:
        from qobuz_librarian.modes.repair import run_album_repair_mode
        raise SystemExit(run_album_repair_mode(args, catalogue_token()))

    if args.upgrade_walk:
        if args.consolidate:
            # The walk switches this off (per-album prompts are unbearable at
            # scale). Say so instead of accepting the flag silently.
            log.info(fmt(C.GRAY,
                "  · The upgrade walk always skips sibling-folder "
                "consolidation; ignoring --consolidate."))
        # AUTO_UPGRADE_ENABLED must stay False as the global default; it
        # controls passive upgrades during ordinary gap-fill walks.
        args.auto_upgrade = True
        from qobuz_librarian.modes.upgrade import run_upgrade_walk_mode
        raise SystemExit(run_upgrade_walk_mode(args, catalogue_token()))

    if args.query:
        from qobuz_librarian.modes.album import run_album_mode
        raise SystemExit(run_album_mode(args, catalogue_token()))

    # Interactive menu loop. The local tools remain reachable without a Qobuz
    # token or downloader; network-backed choices initialise that stack lazily.
    from qobuz_librarian.ui_cli.menu import interactive_session_mode
    from qobuz_librarian.ui_cli.sentinels import Mode
    while True:
        mode = interactive_session_mode()
        if mode == Mode.QUIT:
            return
        uses = (
            mode in (Mode.ALBUM, Mode.ARTIST, Mode.WALK_QUEUE,
                     Mode.ALBUM_WALK, Mode.ALBUM_REPAIR),
            mode in (Mode.UPGRADE, Mode.MIGRATE, Mode.DOWNSAMPLE, Mode.LYRICS),
        )
        if queue_refusal(*uses):
            if offer_saved_queue() is None:
                return
            if queue_refusal(*uses):
                continue
        if mode == Mode.ALBUM:
            # Loop inside album mode so the user can search album after album
            # without bouncing back to the top menu each time.
            from qobuz_librarian.modes.album import run_album_mode
            run_album_mode(args, catalogue_token(), query_args=[], loop=True)
        elif mode == Mode.ARTIST:
            from qobuz_librarian.modes.artist import run_artist_mode
            from qobuz_librarian.ui_cli.prompts import prompt_artist_name
            while True:
                artist = prompt_artist_name()
                if artist is None:
                    log.info(fmt(C.GRAY, "  Cancelled."))
                    break
                run_artist_mode(artist, args, catalogue_token())
        elif mode == Mode.WALK_QUEUE:
            from qobuz_librarian.modes.walk import run_walk_queued_mode
            run_walk_queued_mode(args, catalogue_token())
        elif mode == Mode.ALBUM_WALK:
            from qobuz_librarian.modes.walk import run_album_walk_mode
            run_album_walk_mode(args, catalogue_token())
        elif mode == Mode.ALBUM_REPAIR:
            from qobuz_librarian.modes.repair import run_album_repair_mode
            run_album_repair_mode(args, catalogue_token(), loop=True)
        elif mode == Mode.UPGRADE:
            # Explicit upgrade walk: the user chose this, so enable the
            # upgrade-replace path for its duration regardless of the
            # AUTO_UPGRADE_ENABLED default (which only governs passive
            # upgrades during ordinary gap-fill walks).
            from qobuz_librarian.modes.upgrade import run_upgrade_walk_mode
            saved = getattr(args, "auto_upgrade", cfg.AUTO_UPGRADE_ENABLED)
            args.auto_upgrade = True
            try:
                run_upgrade_walk_mode(args, catalogue_token())
            finally:
                args.auto_upgrade = saved
        elif mode == Mode.MIGRATE:
            from qobuz_librarian.modes.migrate import run_migrate_mode
            run_migrate_mode(args)
        elif mode == Mode.DOWNSAMPLE:
            check_media_tools()
            require_music_root()
            from qobuz_librarian.modes.downsample import run_downsample_walk_mode
            run_downsample_walk_mode(args)
        elif mode == Mode.LYRICS:
            require_music_root()
            from qobuz_librarian.modes.lyrics import run_library_lyrics_mode
            run_library_lyrics_mode(args)
        elif mode == Mode.SETTINGS:
            from qobuz_librarian.modes.settings_cli import run_settings_mode
            run_settings_mode(args)


def _check_staging_occupied():
    """Name the album folders a --no-import run left in staging."""
    # Only while this run still holds the lock: otherwise a run folder may be
    # another writer's download in progress.
    if _RUN_LEASE is None or _RUN_LEASE.intact() is not True:
        return
    from qobuz_librarian import download
    from qobuz_librarian.integrations.staging import kept_staging_run_names
    from qobuz_librarian.queue.startup_recovery import unclaimed_staging_run_names

    def album_dirs_in(roots):
        return [
            album_dir
            for root in roots
            for album_dir in download.staged_album_dirs(root)
        ]

    try:
        if not cfg.STAGING_DIR.exists():
            return
        # A run folder a saved queue claims is still the queue's to finish.
        unclaimed = unclaimed_staging_run_names()
        if unclaimed is None:
            return
        # The next run clears these from staging.
        unfinished = album_dirs_in(cfg.STAGING_DIR / name for name in unclaimed)
        album_dirs = album_dirs_in(
            [cfg.STAGING_DIR / name for name in kept_staging_run_names()] + [
                d for d in sorted(cfg.STAGING_DIR.iterdir())
                if d.is_dir() and not d.name.startswith(".")
            ]) + unfinished
        if album_dirs:
            one = len(album_dirs) == 1
            them = "it" if one else "them"
            location = " from the Compose host" if _in_container() else ""
            notice = wrap(
                f"{plural(len(album_dirs), 'album folder')} in "
                f"{cfg.STAGING_DIR} {'was' if one else 'were'} not imported. "
                + (f"The next run clears incomplete downloads from staging, "
                   f"so import {them} first{location}:"
                   if unfinished else f"Import {them}{location} with:"),
                indent="  ⚠  ", hanging="     ")
            command = "\n".join(_wrap_shell_command(
                _beets_recovery_command(album_dirs), text_width(),
                initial_indent="    ", continuation_indent="      "))
            log.info(fmt(C.YELLOW, f"\n{notice}\n{command}"))
    except OSError:
        pass


def _note_held_staging():
    """Point once, at the end of a run that downloaded, at what staging
    recovery holds."""
    from qobuz_librarian import download

    if (not download.download_ran or _RUN_LEASE is None
            or _RUN_LEASE.intact() is not True):
        return
    from qobuz_librarian.integrations.staging import inspect_retry_groups

    held = len(inspect_retry_groups())
    if held:
        log.info(fmt(C.GRAY, wrap(
            f"{plural(held, 'kept download item')} "
            f"{'is' if held == 1 else 'are'} held in "
            f"{cfg.STAGING_DIR / cfg.BEETS_RETRY_DIR}. Settings > Diagnostics "
            f"in the web app lists {'it' if held == 1 else 'them'}.",
            indent="  ", hanging="  ")))


def _maybe_drop_privileges():
    """Re-exec under gosu to PUID/PGID when started as root. The entrypoint drops
    PID 1 to PUID/PGID, but Docker exec commands bypass the entrypoint.
    """
    import os
    import shutil
    import sys
    if os.geteuid() != 0:
        return
    puid = (os.environ.get("PUID") or "").strip()
    pgid = (os.environ.get("PGID") or "").strip()
    if not puid and not pgid and not _in_container():
        return
    puid = puid or "1000"
    pgid = pgid or "1000"
    if not (puid.isdigit() and pgid.isdigit()):
        return
    uid = int(puid, 10)
    gid = int(pgid, 10)
    if (uid == 0) != (gid == 0):
        die(fmt(C.RED,
            f"\n✗  PUID={uid} PGID={gid} mixes root with a non-root ID.\n"
            "   Use a fully non-root pair, or PUID=0 PGID=0 to run as root.\n"),
            EXIT_CONFIG)
    # A deliberate 0:0 request is already satisfied. Canonicalising before
    # this check also stops values such as 00:00 re-execing as root forever.
    if uid == 0:
        return
    gosu = shutil.which("gosu")
    if not gosu:
        return
    home = os.environ.get("APP_HOME", "/tmp")
    # Reconstruct the invocation.
    import __main__
    spec = getattr(__main__, "__spec__", None)
    if spec is not None and getattr(spec, "name", None):
        mod = spec.name
        if mod.endswith(".__main__"):
            mod = mod[: -len(".__main__")]
        prog = [sys.executable, "-m", mod]
    else:
        prog = [sys.argv[0]]
    os.execvp(gosu, [gosu, f"{uid}:{gid}", "env", f"HOME={home}",
                     *prog, *sys.argv[1:]])


def _entry():
    """Console-script entry point. Centralizes interrupt and AuthLost
    handling so every mode dispatch in main() can let them propagate."""
    _maybe_drop_privileges()
    try:
        try:
            main()
        except KeyboardInterrupt:
            die(fmt(C.GRAY, "\n  Interrupted."), EXIT_INTERRUPT)
        except AuthLost:
            die(fmt(C.RED, auth_lost_msg()), EXIT_AUTH)
        except QobuzUnavailable as e:
            die(fmt(C.YELLOW, f"\n⚠  {e}\n"), EXIT_TRANSIENT)
        except DownloaderNotReady:
            die(fmt(C.RED,
                "\n✗  Your Qobuz token works, but downloads also need your "
                "Qobuz user ID. Add it on the Settings page in the web UI or "
                "set QOBUZ_USER_ID. Nothing changed.\n"),
                EXIT_CONFIG)
        except CredentialChanged:
            die(fmt(C.YELLOW,
                "\n⚠  Qobuz credentials changed while the action was starting. "
                "Nothing changed; run it again.\n"), EXIT_TRANSIENT)
        except QobuzEntitlementError:
            die(fmt(C.RED,
                "\n✗  Qobuz accepted the token, but this account cannot perform "
                "the requested action. Nothing changed.\n"), EXIT_AUTH)
        except PermissionError as e:
            if not e.filename:
                raise
            die(fmt(C.RED,
                f"\n✗  Can't use {e.filename}: permission denied.\n"
                "   Check that it belongs to the user Qobuz Librarian runs as "
                "(PUID and PGID in Docker), then try again.\n"), EXIT_GENERAL)
    finally:
        # Persist any artist resolutions this run discovered (no-op when none),
        # so the next CLI walk skips the search calls; only the web flows
        # flushed before, leaving every CLI walk to re-resolve from cold.
        from qobuz_librarian.library.discovery import flush_resolve_cache
        flush_resolve_cache()
        _check_staging_occupied()
        _note_held_staging()


if __name__ == "__main__":
    _entry()
