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

from qobuz_librarian import __version__, run_lock
from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import (
    AuthLost,
    NoCredsError,
    QobuzUnavailable,
    load_qobuz_token,
)
from qobuz_librarian.integrations.lyrics import _prune_lyric_state_orphans
from qobuz_librarian.integrations.rip import HAVE_MUTAGEN
from qobuz_librarian.library.backup import cleanup_old_upgrade_backups
from qobuz_librarian.quality.tiers import streamrip_quality_cap
from qobuz_librarian.queue.persistence import (
    offer_resume_pending_queue,
    offer_resume_startup_recovery,
)
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
    EXIT_LOCK_BUSY,
    EXIT_TRANSIENT,
    die,
)
from qobuz_librarian.ui_cli.logging import attach_file_handler, log, set_quiet, set_verbose, vlog

_STARTUP_RECOVERY_RESULT = None

# ── URL parsers ───────────────────────────────────────────────────────────────

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


# ── Single-instance lock ──────────────────────────────────────────────────────

def _recover_startup_queue(authority):
    """Inspect durable queue state without starting download/import work."""
    from qobuz_librarian.queue.startup_recovery import recover_startup_state

    return recover_startup_state(authority=authority)


def _record_startup_recovery(authority):
    global _STARTUP_RECOVERY_RESULT
    _STARTUP_RECOVERY_RESULT = _recover_startup_queue(authority)
    return _STARTUP_RECOVERY_RESULT


def _startup_recovery_status():
    return getattr(_STARTUP_RECOVERY_RESULT, "status", None)


def _cli_blocked_settlement_binding(result):
    """Bind one settleable block to its frozen CLI completion owner."""
    from qobuz_librarian.completion import (
        CompletionOriginKind,
        RecoveryOwner,
        normalise_album_id,
        parse_completion_input_record,
    )
    from qobuz_librarian.queue import journal as queue_state
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryAction,
        StartupRecoveryStatus,
        settleable_block_kind,
    )

    items = tuple(getattr(result, "items", ()))
    blocked = tuple(
        item
        for item in items
        if item.phase is queue_state.QueuePhase.BLOCKED
        and item.action is StartupRecoveryAction.BLOCKED
    )
    if (
        result.status is not StartupRecoveryStatus.ATTENTION_REQUIRED
        or result.reason != "queue-item-blocked"
        or len(blocked) != 1
        or not items
        or any(
            item.operation_id != blocked[0].operation_id
            or item.mode != blocked[0].mode
            or (
                item is not blocked[0]
                and (
                    item.phase is not queue_state.QueuePhase.PENDING
                    or item.action is not StartupRecoveryAction.PENDING
                )
            )
            for item in items
        )
    ):
        return None
    target = blocked[0]
    if target.mode.startswith("web-job:"):
        return None
    loaded = queue_state.load_queue_journal(target.operation_id)
    journal = loaded.journal
    if (
        loaded.status is not queue_state.QueueLoadStatus.READY
        or journal is None
        or journal.operation_id != target.operation_id
        or journal.mode != target.mode
        or journal.retirements
        or len(journal.items) != len(items)
    ):
        return None
    classified = {item.item_id: item for item in items}
    if len(classified) != len(items):
        return None
    for queued in journal.items:
        recovered = classified.get(queued.item_id)
        if recovered is None or recovered.phase is not queued.phase:
            return None
        expected_action = (
            StartupRecoveryAction.BLOCKED
            if queued.item_id == target.item_id
            else StartupRecoveryAction.PENDING
        )
        if recovered.action is not expected_action:
            return None
    queued = next(
        (item for item in journal.items if item.item_id == target.item_id),
        None,
    )
    if queued is None:
        return None
    settleable = settleable_block_kind(queued)
    if settleable is None:
        return None
    completion_input = parse_completion_input_record(
        queued.completion_input,
        expected_owner=RecoveryOwner(target.operation_id, target.item_id),
    )
    planned_album = queued.planned.get("album")
    if (
        completion_input is None
        or completion_input.origin.kind is not CompletionOriginKind.CLI
        or completion_input.origin.reference != "download-queue"
        or not isinstance(planned_album, dict)
        or normalise_album_id(planned_album.get("id"))
        != normalise_album_id(completion_input.expectation.album_id)
    ):
        return None
    label = planned_album.get("title") or queued.planned.get("label") or "download"
    return target, str(label), settleable


def _cli_retry_settlement_matches(result, target) -> bool:
    from qobuz_librarian.queue import journal as queue_state
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryAction,
        StartupRecoveryStatus,
    )

    if result.status is not StartupRecoveryStatus.RESUME_REQUIRED:
        return False
    recovered = next(
        (
            item
            for item in result.items
            if item.operation_id == target.operation_id
            and item.item_id == target.item_id
            and item.mode == target.mode
        ),
        None,
    )
    loaded = queue_state.load_queue_journal(target.operation_id)
    if (
        recovered is None
        or recovered.phase is not queue_state.QueuePhase.PENDING
        or recovered.action is not StartupRecoveryAction.PENDING
        or loaded.status is not queue_state.QueueLoadStatus.READY
        or loaded.journal is None
        or loaded.journal.mode != target.mode
    ):
        return False
    queued = next(
        (
            item
            for item in loaded.journal.items
            if item.item_id == target.item_id
        ),
        None,
    )
    return (
        queued is not None
        and queued.phase is queue_state.QueuePhase.PENDING
        and not queued.recovery_references
        and queued.block_reason is None
        and queued.completion_input is None
        and queued.completion_evidence is None
    )


def _cli_discard_settlement_matches(result, target) -> bool:
    from qobuz_librarian.queue import journal as queue_state
    from qobuz_librarian.queue.startup_recovery import StartupRecoveryStatus

    if result.status not in {
        StartupRecoveryStatus.CLEAR,
        StartupRecoveryStatus.RESUME_REQUIRED,
    } or any(
        item.operation_id == target.operation_id
        and item.item_id == target.item_id
        for item in result.items
    ):
        return False
    loaded = queue_state.load_queue_journal(target.operation_id)
    return loaded.status is queue_state.QueueLoadStatus.ABSENT or (
        loaded.status is queue_state.QueueLoadStatus.READY
        and loaded.journal is not None
        and loaded.journal.mode == target.mode
        and all(
            item.item_id != target.item_id for item in loaded.journal.items
        )
    )


def _cli_settlement_cleared_recovery(result, target) -> bool:
    """True when a refused settlement left nothing for this item to settle."""
    from qobuz_librarian.queue.startup_recovery import StartupRecoveryStatus

    return result.status is StartupRecoveryStatus.CLEAR and all(
        item.operation_id != target.operation_id
        or item.item_id != target.item_id
        for item in getattr(result, "items", ())
    )


def _cli_blocked_item_settled(result, target) -> bool:
    """True when this exact item is no longer blocked, whatever else is.

    Clearing a staged leftover can leave the rest of the recovery to reconcile
    on the next pass, so the whole-recovery answer is not this item's answer.
    """
    from qobuz_librarian.queue import journal as queue_state

    return all(
        item.operation_id != target.operation_id
        or item.item_id != target.item_id
        or item.phase is not queue_state.QueuePhase.BLOCKED
        for item in getattr(result, "items", ())
    )


def _offer_blocked_cli_settlement(authority, result):
    """Offer an explicit decision for one settleable blocked download."""
    from qobuz_librarian.queue.startup_recovery import (
        SETTLEABLE_STAGED_LEFTOVER,
        BlockedItemSettlementAction,
        BlockedItemSettlementStatus,
        settle_blocked_item,
    )

    binding = _cli_blocked_settlement_binding(result)
    if binding is None:
        return result, False, None
    item, label, settleable = binding
    leftover = settleable == SETTLEABLE_STAGED_LEFTOVER

    if leftover:
        # The file in staging holds the queue, not the saved entry, so a retry
        # and a discard would both name the same act. Two choices, not three.
        prompt = (
            f"\n  “{label}” left something behind in the staging folder, and "
            "downloads and scans\n"
            "  stay paused until it is cleared.\n"
            "  Clear it now, or keep it for later?\n"
            "  Choice [c=clear now, Enter=keep]: "
        )
        retry_words = {"c", "clear", "r", "retry"}
        again = "  Enter c to clear it, or press Enter to keep it: "
    else:
        prompt = (
            f"\n  The interrupted download “{label}” stopped before Beets "
            "changed the library.\n"
            "  Retry it, keep it blocked for later, or discard its saved queue "
            "entry?\n"
            "  Choice [r=retry, Enter=keep, d=discard]: "
        )
        retry_words = {"r", "retry"}
        again = "  Enter r to retry, d to discard, or press Enter to keep it: "
    while True:
        try:
            choice = input(fmt(C.CYAN, prompt)).strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = ""
        if choice in {"", "k", "keep"}:
            return result, True, None
        if choice in retry_words:
            action = BlockedItemSettlementAction.RETRY
            expected = BlockedItemSettlementStatus.RETRYABLE
            break
        if not leftover and choice in {"d", "discard"}:
            try:
                confirmed = input(fmt(
                    C.RED,
                    "  Type DISCARD to remove this saved queue entry: ",
                )).strip()
            except (EOFError, KeyboardInterrupt):
                confirmed = ""
            if confirmed != "DISCARD":
                return result, True, None
            action = BlockedItemSettlementAction.DISCARD
            expected = BlockedItemSettlementStatus.DISCARDED
            break
        prompt = again

    cleared = fmt(C.GREEN,
                  "  ✓ Cleared the leftover that was blocking this download.")
    try:
        settled = settle_blocked_item(
            authority=authority,
            operation_id=item.operation_id,
            item_id=item.item_id,
            action=action,
        )
        fresh = _record_startup_recovery(authority)
    except Exception:
        return result, False, None
    if settled.status is not expected:
        # A refusal still parks the item's stranded staging, and for a download
        # that already imported that is the whole of what was outstanding.
        # Reporting the refusal over a recovery it just cleared would strand the
        # run behind a stale verdict.
        if _cli_settlement_cleared_recovery(fresh, item):
            log.info(cleared)
            return fresh, False, None
        if _cli_blocked_item_settled(fresh, item):
            # The blocker is gone but the rest of the recovery reconciles on
            # the next pass, so one more read can still come back clear.
            rechecked = _record_startup_recovery(authority)
            if _cli_settlement_cleared_recovery(rechecked, item):
                log.info(cleared)
                return rechecked, False, None
            return rechecked, False, (
                f"\n✓  Cleared the leftover that was blocking “{label}”.\n"
                "   Restart Qobuz Librarian to finish settling it. Nothing "
                "else was started.\n"
            )
        log.info(fmt(C.YELLOW, f"  {settled.reason}"))
        return result, False, None
    verified = (
        _cli_retry_settlement_matches(fresh, item)
        if action is BlockedItemSettlementAction.RETRY
        else _cli_discard_settlement_matches(fresh, item)
    )
    return (fresh, False, None) if verified else (result, False, None)


def _die_unsettled_startup_recovery(
    authority,
    result=None,
    *,
    kept: bool = False,
    note: str | None = None,
) -> None:
    try:
        authority.close()
    except OSError:
        pass
    if note is not None:
        # The decision landed but the run still cannot carry on, so the stop
        # reports what succeeded rather than the generic failure below.
        die(fmt(C.YELLOW, note), EXIT_GENERAL)
    if kept:
        message = (
            "\n  The interrupted download was kept for later.\n"
            "   Its saved queue entry was left unchanged and no other work "
            "was started.\n"
        )
        color = C.YELLOW
    elif (
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
    else:
        # A state this stop is reached in survives a restart, so the message
        # names where the outstanding work is instead of prescribing one.
        message = (
            "\n✗  An interrupted download could not be verified safely.\n"
            "   The saved queue and staged files were left unchanged, and no "
            "other work was started.\n"
            f"   What is still outstanding is under {cfg.STAGING_DIR}, and the "
            "application log names the exact blocked recovery reason.\n"
        )
        color = C.RED
    die(fmt(color, message), EXIT_GENERAL)


def _compose_service_name() -> str:
    """Best-effort docker compose service name for diagnostic hints.

    Docker sets HOSTNAME to the container's name when `container_name:` is
    set in compose, else to the 12-char container ID. Fall back to the
    generic name when we see an ID, so user-facing strings don't print a
    misleading hex blob.
    """
    import os
    h = os.environ.get("HOSTNAME", "").strip()
    if not h or (len(h) == 12 and all(c in "0123456789abcdef" for c in h)):
        return "qobuz-librarian"
    return h


def acquire_run_lock():
    """Acquire the single-writer run lock or exit."""
    try:
        lease = run_lock.acquire()
    except run_lock.LockBusy as busy:
        _svc = _compose_service_name()
        die(fmt(C.RED,
            f"\n✗  Another Qobuz Librarian run is in progress (pid {busy.pid}).\n"
            f"   Lock file: {cfg.LOCK_FILE}\n\n"
            f"   The web app may be holding the lock. Either:\n"
            f"     1. In the web UI (http://<host>:{cfg.WEB_PORT}), open Settings → Mode\n"
            f"        and switch to terminal mode, then re-run this command.\n"
            f"        (Or just use the web UI; every CLI mode is also a web action.)\n"
            f"     2. Stop the web container instead:  docker compose stop {_svc}\n"
            f"        then re-run, then `docker compose start {_svc}`.\n\n"
            f"   Only one writer can use /staging at a time.\n"),
            EXIT_LOCK_BUSY)
    if lease is not None and lease.intact() is True:
        try:
            result = _record_startup_recovery(lease)
        except Exception as exc:
            try:
                lease.close()
            except OSError as close_exc:
                vlog(f"run-lock release after recovery failure: {close_exc}")
            vlog(f"startup recovery check failed: {exc}")
            die(fmt(
                C.RED,
                "\n✗  The saved recovery state could not be checked safely.\n"
                "   The safety lock was released and no library work was "
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
        kept = False
        note = None
        if result.status is StartupRecoveryStatus.ATTENTION_REQUIRED:
            result, kept, note = _offer_blocked_cli_settlement(lease, result)
        if result.status is StartupRecoveryStatus.ATTENTION_REQUIRED:
            _die_unsettled_startup_recovery(lease, result, kept=kept, note=note)
        return lease
    if lease is not None:
        lease.close()
    die(fmt(
        C.RED,
        "\n✗  The single-writer safety lock could not be established.\n"
        f"   Lock file: {cfg.LOCK_FILE}\n\n"
        "   Refusing to modify staging or the library without exclusive "
        "write authority. Check the data-volume permissions and filesystem "
        "locking support, then try again.\n",
    ), EXIT_GENERAL)


# ── Pre-flight checks ─────────────────────────────────────────────────────────

def _in_container() -> bool:
    import os
    return os.path.exists("/.dockerenv") or os.environ.get("QL_IN_CONTAINER") == "1"


def _missing_tool_hint(tool: str, install_hint: str) -> str:
    if _in_container():
        return (f"\n✗  `{tool}` not in PATH inside the container.\n"
                f"   This means the image is broken. Rebuild with "
                f"`docker compose build --no-cache`.\n")
    return f"\n✗  `{tool}` not in PATH. {install_hint}\n"


def check_rip():
    try:
        r = subprocess.run(["rip", "--version"], capture_output=True, text=True, timeout=10)
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


def require_music_root():
    if not cfg.MUSIC_ROOT.exists() or not cfg.MUSIC_ROOT.is_dir():
        die(fmt(C.RED,
            f"\n✗  MUSIC_ROOT missing or inaccessible: {cfg.MUSIC_ROOT}\n"
            "   Refusing to proceed.\n"), EXIT_CONFIG)


# ── Argument parsing ──────────────────────────────────────────────────────────

class _ExitOneArgParser(argparse.ArgumentParser):
    """ArgumentParser that exits 1 (EXIT_GENERAL) on parse errors instead
    of argparse's default 2. Our documented exit-code contract reserves 2
    for EXIT_AUTH; without this override, a `--flag --conflict` typo
    surfaces with the same code as "token expired" and cron retry rules
    can't tell them apart.
    """

    def exit(self, status=0, message=None):
        if status == 2:
            status = EXIT_GENERAL
        return super().exit(status, message)


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, width=text_width())

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
    tokens = re.findall(r"'[^']*'|\"[^\"]*\"|\S+", command)
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


def _beets_recovery_command():
    staging = shlex.quote(str(cfg.STAGING_DIR))
    if _in_container():
        service = shlex.quote(_compose_service_name())
        return f"docker compose run --rm {service} beet import {staging}"
    return f"beet import {staging}"


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
        f"On a fresh install, open http://<host>:{cfg.WEB_PORT}/settings.",
        width=width,
        break_long_words=False,
        break_on_hyphens=False,
    ))
    lines.extend(("", "Exit codes:"))
    for code, description in (
        ("0", "success"),
        ("1", "general failure (including interrupt)"),
        ("2", "authentication token invalid or missing"),
        ("3", "another writer holds the run lock"),
        ("4", "transient network or API error; retry later"),
        ("64", "configuration or required tool missing"),
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
        usage="%(prog)s [options]\n       [query ...]",
        description=_help_description(),
        formatter_class=_HelpFormatter,
        epilog=_help_epilog())
    p.add_argument("--version", action="version", version=f"qobuz-librarian {__version__}")
    p.add_argument("query", nargs="*", help="search query or Qobuz album URL")
    p.add_argument("--artist",       metavar="NAME",
                   help="run artist mode on NAME (skips interactive menu)")
    p.add_argument("--library-walk", action="store_true",
                   help="walk every artist: fill gaps, then offer albums you're "
                        "missing; queue as you go (same as menu Library walk).")
    p.add_argument("--album-gaps", action="store_true",
                   help="fill missing tracks in every incomplete album you own; "
                        "never suggests albums you don't have.")
    p.add_argument("--repair", action="store_true",
                   help="re-download damaged (truncated) tracks you own. '*' at "
                        "the artist prompt sweeps the whole library.")
    p.add_argument("--upgrade-walk", action="store_true",
                   help="review saved Library upgrade candidates. Per-artist "
                        "confirm (enter=skip), auto-advance.")
    p.add_argument("--downsample-walk", action="store_true",
                   help="scan the library for hi-res files and downsample them "
                        "to CD rate in place (per-artist confirm; --dry-run lists "
                        "only). Local; no Qobuz login needed.")
    p.add_argument("--check-new-releases", dest="check_new_releases",
                   action="store_true",
                   help="walk library artists and report what's new on Qobuz "
                        "since the last check (same engine the web auto-check "
                        "uses); --dry-run skips advancing the baseline")
    p.add_argument("--lyrics-walk", action="store_true",
                   help="fetch lyrics for tracks already in the library that "
                        "are missing them (LYRICS_FORMAT / LYRICS_PROVIDERS "
                        "settings apply). Local; no Qobuz login needed.")
    p.add_argument("--lyrics-rescan", action="store_true",
                   help="with --lyrics-walk: re-check every track, ignoring the "
                        "saved per-track state.")
    p.add_argument("--lyrics-synced-only", action="store_true",
                   help="with --lyrics-walk: only write timed (synced) lyrics, "
                        "never plain.")
    p.add_argument("--no-catalog",   action="store_true",
                   help="skip missing album suggestions in Artist mode and "
                        "Library walk")
    p.add_argument("--include-comps", action="store_true",
                   help="include compilation/various-artists releases in "
                        "missing album suggestions for Artist mode and "
                        "Library walk")
    p.add_argument("--dry-run",      action="store_true", help="show plan, download nothing")
    p.add_argument("--no-import",    action="store_true",
                   help="download but skip beets import; see the recovery "
                        "command below")
    p.add_argument("--force",        action="store_true",
                   help="redownload everything (album mode only)")
    # Default comes from config (PREFER_HIRES, env/Settings overridable) so
    # CLI and web behave the same. --no-prefer-hires overrides per-run.
    p.add_argument("--prefer-hires", dest="prefer_hires",
                   action=argparse.BooleanOptionalAction,
                   default=cfg.PREFER_HIRES,
                   help="sort 24-bit / higher sample rate results first")
    p.add_argument("--yes",          action="store_true",
                   help="auto-confirm download/import prompts "
                        "(destructive prompts still ask)")
    p.add_argument("--verbose",      action="store_true", help="show detection details")
    # Default from config (CONSOLIDATE, env/CLI overridable).
    p.add_argument("--consolidate",  dest="consolidate",
                   action=argparse.BooleanOptionalAction,
                   default=cfg.CONSOLIDATE,
                   help="after import, scan sibling folders and offer to consolidate")
    # Passive auto-upgrade is OFF unless AUTO_UPGRADE_ENABLED is set or the
    # explicit Upgrade walk is run.
    p.add_argument("--no-upgrade",   action="store_true",
                   help="force-disable quality upgrades for this run (plain gap-fill)")
    # Unattended upgrade-walk gate.
    p.add_argument("--auto-safe",    action="store_true",
                   help="auto-confirm only safe candidates (requires --upgrade-walk).")
    # Missing-album noise filter: hide/show short releases.
    p.add_argument("--include-singles", action="store_true",
                   help=f"include releases with fewer than {cfg.MISSING_ALBUMS_MIN_TRACKS} tracks "
                        "in missing album suggestions for Artist mode and "
                        "Library walk")
    p.add_argument("--no-color",     action="store_true", help="disable ANSI colours")
    p.add_argument("--quiet", "-q",  action="store_true",
                   help="suppress info-level console output (warnings/errors "
                        "still print; the log file keeps recording)")
    p.add_argument("--reset-walk-seen", action="store_true",
                   help="delete the library-walk dedup files and exit "
                        "(so the next walk revisits every artist/album)")
    p.add_argument("--no-downsample",  dest="no_downsample",
                   action="store_true",
                   help="force-skip pre-import downsampling for this run "
                        "(only relevant when downsampling is enabled)")
    p.add_argument("--migrate-multi-artist", dest="migrate_multi_artist",
                   action=argparse.BooleanOptionalAction,
                   default=cfg.MIGRATE_MULTI_ARTIST,
                   help="after import, safely re-file 'Primary, Other' albums "
                        "under 'Primary'")
    p.add_argument("--migrate", action="store_true",
                   help="one-time setup: reorganise an existing library into the "
                        "Artist/Album layout (local-only; no Qobuz login needed)")
    p.add_argument("--migrate-src", dest="migrate_src", metavar="PATH", default="",
                   help="migration source library to read (overrides MIGRATE_SRC)")
    p.add_argument("--migrate-dest", dest="migrate_dest", metavar="PATH", default="",
                   help="where migration builds the organised copy "
                        "(overrides MIGRATE_DEST)")
    p.add_argument("--in-place", dest="in_place", action="store_true",
                   help="migration: MOVE files into place instead of copying "
                        "(default copies, leaving originals untouched)")
    p.add_argument("--acoustid", dest="acoustid", action="store_true",
                   help="migration: fingerprint files whose tags can't place them "
                        "(slower, needs network; no key required)")
    args = p.parse_args()
    # Per-run override of cfg.AUTO_UPGRADE_ENABLED.
    args.auto_upgrade = cfg.AUTO_UPGRADE_ENABLED
    # The walk / migrate / reset modes are whole-library or local-only runs that
    # read none of the album- or artist-scan flags below, so naming one of those
    # flags alongside them would silently drop it.
    other_run_mode = (args.upgrade_walk or args.downsample_walk
                      or args.lyrics_walk or args.migrate or args.reset_walk_seen
                      or args.check_new_releases or args.library_walk
                      or args.album_gaps or args.repair)
    # Reject flag/mode combinations that would otherwise be silently dropped
    # or accepted.
    if args.force and (args.artist or other_run_mode):
        p.error("--force only applies to album mode (a query or Qobuz URL), "
                "not --artist or a walk/migrate mode")
    # --migrate's extra options mean nothing without it.
    if (args.in_place or args.acoustid or args.migrate_src or args.migrate_dest) \
            and not args.migrate:
        p.error("--in-place / --acoustid / --migrate-src / --migrate-dest only "
                "apply with --migrate")
    # --include-singles only affects the missing-albums step of artist mode;
    # which the library walk runs per artist, so it applies there too.
    if (args.include_singles and not (args.artist or args.library_walk)
            and (args.query or other_run_mode)):
        p.error("--include-singles only applies to artist mode and "
                "--library-walk")
    # --no-catalog skips that same missing-albums step.
    if (args.no_catalog and not (args.artist or args.library_walk)
            and (args.query or other_run_mode)):
        p.error("--no-catalog only applies to artist mode and --library-walk")
    # The upgrade walk reviews saved Library candidates; a query would be
    # silently ignored, so reject it instead of surprising the user.
    if args.upgrade_walk and args.query:
        p.error("--upgrade-walk reviews saved Library candidates. Drop the "
                "query, or run a normal search without --upgrade-walk")
    # --artist dispatches before the positional query, so extra words after the
    # artist name would be silently dropped. Reject so the user picks one.
    if args.artist and args.query:
        p.error("--artist NAME scans that one artist. Drop the extra words, "
                "or search them as an album without --artist")
    # Exactly one whole-run mode executes per invocation; main() dispatches
    # the first it finds and returns, so a second would be silently dropped.
    requested = [name for name, on in (
        ("a query (album mode)", bool(args.query)),
        ("--artist", args.artist is not None),
        ("--library-walk", args.library_walk),
        ("--album-gaps", args.album_gaps),
        ("--repair", args.repair),
        ("--upgrade-walk", args.upgrade_walk),
        ("--downsample-walk", args.downsample_walk),
        ("--lyrics-walk", args.lyrics_walk),
        ("--migrate", args.migrate),
        ("--reset-walk-seen", args.reset_walk_seen),
        ("--check-new-releases", args.check_new_releases),
    ) if on]
    if len(requested) > 1:
        p.error("run one mode at a time; got " + ", ".join(requested))
    # With no mode, the run falls through to the interactive menu, whose
    # prompts --quiet would silence, leaving a bare input cursor.
    if args.quiet and not requested:
        p.error("--quiet is for unattended runs and silences the interactive "
                "menu. Name a mode (a query, --artist, --upgrade-walk, …) "
                "or drop --quiet")
    # --include-comps controls compilation filtering in artist mode (and the
    # library walk's per-artist pass).
    if (args.include_comps and not (args.artist or args.library_walk)
            and (args.query or other_run_mode)):
        p.error("--include-comps only applies to artist mode and "
                "--library-walk")
    # --no-upgrade with --upgrade-walk is contradictory.
    if args.no_upgrade and args.upgrade_walk:
        p.error("--no-upgrade conflicts with --upgrade-walk")
    # --auto-safe only gates the unattended upgrade walk.
    if args.auto_safe and not args.upgrade_walk and (
            args.query or args.artist or other_run_mode):
        p.error("--auto-safe only applies to --upgrade-walk")
    # An empty --artist (e.g. `--artist "$VAR"` with VAR unset) is falsy, so it
    # would silently fall through to the interactive menu instead of running
    # artist mode, which would be confusing in a script. Reject it.
    if args.artist is not None and not args.artist.strip():
        p.error("--artist needs an artist name")
    if (args.lyrics_rescan or args.lyrics_synced_only) and not args.lyrics_walk:
        p.error("--lyrics-rescan / --lyrics-synced-only only apply with "
                "--lyrics-walk")
    return args


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Apply the web Settings page's persisted overrides before parse_args
    # reads cfg.* into the default flags.
    try:
        from qobuz_librarian.web import settings_store
        settings_store.load()
    except Exception:
        pass

    args = parse_args()
    set_verbose(args.verbose)
    set_quiet(args.quiet)
    # role="cli" → a separate log file so a `docker exec` CLI run sharing the
    # container with the long-lived web server can't race it on log rollover.
    attach_file_handler(cfg.APP_LOG_FILE, cfg.LOG_LEVEL, role="cli")
    try:
        cfg.validate_storage_roots()
    except ValueError as exc:
        die(fmt(
            C.RED,
            f"\n✗  Invalid storage paths: {exc}\n"
            "   Refusing to proceed.\n",
        ), EXIT_CONFIG)
    if args.quiet:
        set_color_enabled(False)

    if args.no_color:
        set_color_enabled(False)

    if args.reset_walk_seen and args.dry_run:
        present = [str(f) for f in (cfg.WALK_SEEN_FILE, cfg.ALBUM_WALK_SEEN_FILE)
                   if f.exists()]
        if present:
            log.info(fmt(C.GRAY, "  --dry-run: would clear walk-seen state:"))
            for r in present:
                log.info(fmt(C.GRAY, f"     {r}"))
        else:
            log.info(fmt(C.GRAY, "  No walk-seen state to clear."))
        return

    banner("Qobuz Librarian: search · artist · library · repair · upgrade")

    # Single-instance lock first; fail fast before doing any other work.
    # Hold the file handle for the lifetime of main() so the lock persists.
    _lockfile = acquire_run_lock()  # noqa: F841

    from qobuz_librarian.queue.startup_recovery import StartupRecoveryStatus
    resume_interrupted_queue = (
        _startup_recovery_status() is StartupRecoveryStatus.RESUME_REQUIRED)
    if resume_interrupted_queue and any((
        args.reset_walk_seen,
        args.migrate,
        args.lyrics_walk,
        args.check_new_releases,
        args.downsample_walk,
        args.artist,
        args.library_walk,
        args.album_gaps,
        args.repair,
        args.upgrade_walk,
        args.query,
    )):
        die(fmt(
            C.YELLOW,
            "\n⚠  An interrupted download must be resumed before other work.\n"
            "   Run Qobuz Librarian without a mode or search argument and "
            "choose Resume when it offers the saved queue.\n",
        ), EXIT_GENERAL)

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
            log.info(fmt(C.GREEN, "  ✓  Cleared walk-seen state:"))
            for r in removed:
                log.info(fmt(C.GRAY, f"     {r}"))
        else:
            log.info(fmt(C.GRAY, "  No walk-seen state to clear."))
        return

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

    # The new-release check hits the Qobuz API but doesn't need streamrip,
    # ffmpeg or the FLAC tools (one catalog call per artist, no track
    # fetches), so dispatch here before the heavy tool checks.
    if args.check_new_releases:
        require_music_root()
        from qobuz_librarian.modes.new_releases import (
            run_check_new_releases_mode,
        )
        run_check_new_releases_mode(args)
        return

    # Downsample walk is local-only; it reads hi-res files off disk and
    # resamples them in place, never touching Qobuz.
    if args.downsample_walk:
        check_media_tools()
        require_music_root()
        from qobuz_librarian.modes.downsample import run_downsample_walk_mode
        raise SystemExit(run_downsample_walk_mode(args))

    # Keep the interactive menu and saved-work choices available on a local-
    # only box.
    download_stack = {}

    def download_token():
        if "token" in download_stack:
            return download_stack["token"]
        check_rip()
        check_media_tools()
        require_music_root()

        from qobuz_librarian.api.auth import verify_streamrip_downloads_folder
        verify_streamrip_downloads_folder()
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
        try:
            user_id, token = load_qobuz_token()
        except NoCredsError:
            die(fmt(C.RED,
                "\n✗  No Qobuz credentials configured.\n"
                "   Paste your user_auth_token on the Settings page "
                f"(http://<host>:{cfg.WEB_PORT}/settings)\n"
                "   or set QOBUZ_USER_AUTH_TOKEN in your environment.\n"),
                EXIT_AUTH)
        from qobuz_librarian.api.auth import sync_streamrip_creds_from_env
        if sync_streamrip_creds_from_env() is False:
            log.info(fmt(C.YELLOW,
                "  ⚠  Couldn't write env credentials into the streamrip "
                f"config ({cfg.STREAMRIP_CONFIG}); downloads may fail."))
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
            log.info(fmt(C.GRAY, f"  log file:   {cfg.FETCH_LOG_FILE}"))
            log.info(fmt(C.GRAY, f"  lock:       {cfg.LOCK_FILE}"))
        cap_depth, cap_rate = streamrip_quality_cap()
        vlog(f"streamrip quality cap: {cap_depth}-bit/{cap_rate/1000:g}kHz")
        download_stack["token"] = token
        return token

    if resume_interrupted_queue:
        # Keep this launch recovery-only.
        try:
            offer_resume_startup_recovery(
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
        if result.status is StartupRecoveryStatus.RESUME_REQUIRED:
            log.info(fmt(
                C.YELLOW,
                "  Interrupted queue kept. Other work remains paused until it "
                "is resumed.",
            ))
        return

    # Sweep upgrade-backup dir of anything older than retention window.
    # Cheap (just stat + rmtree on stale dirs); silent unless something happens.
    try:
        n_swept = cleanup_old_upgrade_backups()
        if n_swept:
            log.info(fmt(C.GRAY,
                f"  ⟳  Cleaned up {n_swept} old upgrade backup(s) "
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
    # ── Decide the entry mode ─────────────────────────────────────────────────
    # Single-shot flag paths first (each skips the menu loop), then positional
    # args / URL → album mode, then the interactive menu. The single-shot paths
    # still respect AuthLost / KeyboardInterrupt cleanly; all caught at the
    # bottom by main()'s wrapper.
    if args.artist:
        from qobuz_librarian.modes.artist import run_artist_mode
        run_artist_mode(args.artist, args, download_token())
        return

    if args.library_walk:
        from qobuz_librarian.modes.walk import run_walk_queued_mode
        run_walk_queued_mode(args, download_token())
        return

    if args.album_gaps:
        from qobuz_librarian.modes.walk import run_album_walk_mode
        run_album_walk_mode(args, download_token())
        return

    if args.repair:
        from qobuz_librarian.modes.repair import run_album_repair_mode
        run_album_repair_mode(args, download_token())
        return

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
        raise SystemExit(run_upgrade_walk_mode(args, download_token()))

    if args.query:
        from qobuz_librarian.modes.album import run_album_mode
        run_album_mode(args, download_token())
        return

    # Crash-recovery: if a previous queueing run died with decisions still in
    # memory, we'd have left .qobuz_pending_queue.json on disk.
    offer_resume_pending_queue(args, download_token)

    # Same idea, but for files where every lyric provider was unavailable last
    # run.
    from qobuz_librarian.integrations.lyrics import offer_resume_lyric_retry
    offer_resume_lyric_retry(args)

    # Interactive menu loop. The local tools remain reachable without a Qobuz
    # token or downloader; network-backed choices initialise that stack lazily.
    from qobuz_librarian.ui_cli.menu import interactive_session_mode
    from qobuz_librarian.ui_cli.sentinels import Mode
    while True:
        mode = interactive_session_mode()
        if mode == Mode.QUIT:
            log.info(fmt(C.GRAY, "  Bye."))
            return
        if mode == Mode.ALBUM:
            # Loop inside album mode so the user can search album after album
            # without bouncing back to the top menu each time.
            from qobuz_librarian.modes.album import run_album_mode
            run_album_mode(args, download_token(), query_args=[], loop=True)
        elif mode == Mode.ARTIST:
            from qobuz_librarian.modes.artist import run_artist_mode
            from qobuz_librarian.ui_cli.prompts import prompt_artist_name
            while True:
                artist = prompt_artist_name()
                if artist is None:
                    log.info(fmt(C.GRAY, "  Cancelled."))
                    break
                run_artist_mode(artist, args, download_token())
        elif mode == Mode.WALK_QUEUE:
            from qobuz_librarian.modes.walk import run_walk_queued_mode
            run_walk_queued_mode(args, download_token())
        elif mode == Mode.ALBUM_WALK:
            from qobuz_librarian.modes.walk import run_album_walk_mode
            run_album_walk_mode(args, download_token())
        elif mode == Mode.ALBUM_REPAIR:
            from qobuz_librarian.modes.repair import run_album_repair_mode
            run_album_repair_mode(args, download_token(), loop=True)
        elif mode == Mode.UPGRADE:
            # Explicit upgrade walk: the user chose this, so enable the
            # upgrade-replace path for its duration regardless of the
            # AUTO_UPGRADE_ENABLED default (which only governs passive
            # upgrades during ordinary gap-fill walks).
            from qobuz_librarian.modes.upgrade import run_upgrade_walk_mode
            saved = getattr(args, "auto_upgrade", cfg.AUTO_UPGRADE_ENABLED)
            args.auto_upgrade = True
            try:
                run_upgrade_walk_mode(args, download_token())
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


def _check_staging_occupied():
    """Warn if STAGING_DIR has content left behind by a --no-import run or crash."""
    try:
        if not cfg.STAGING_DIR.exists():
            return
        subdirs = [
            d for d in cfg.STAGING_DIR.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        ]
        if subdirs:
            location = " from the Compose host" if _in_container() else ""
            notice = wrap(
                f"{len(subdirs)} album folder(s) remain in {cfg.STAGING_DIR}. "
                f"Keep other downloads paused, then run{location}:",
                indent="  ⚠  ", hanging="     ")
            command = "\n".join(_wrap_shell_command(
                _beets_recovery_command(), text_width(),
                initial_indent="    ", continuation_indent="      "))
            log.info(fmt(C.YELLOW, f"\n{notice}\n{command}"))
    except OSError:
        pass


def _maybe_drop_privileges():
    """Re-exec under gosu to PUID/PGID when started as root. The entrypoint drops
    PID 1 to PUID/PGID, but Docker exec commands bypass the entrypoint.
    """
    import os
    import shutil
    import sys
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
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
            die(fmt(C.GRAY, "\n  Interrupted."), EXIT_GENERAL)
        except AuthLost:
            die(fmt(C.RED,
                "\n✗  Auth lost. Re-authenticate: Settings page in the web UI, "
                "or set QOBUZ_USER_AUTH_TOKEN in your environment.\n"), EXIT_AUTH)
        except QobuzUnavailable as e:
            die(fmt(C.YELLOW,
                f"\n⚠  Qobuz is temporarily unavailable: {e}\n"
                "   Nothing was lost; any queued work was saved. Re-run when it's "
                "back.\n"), EXIT_TRANSIENT)
    finally:
        # Persist any artist resolutions this run discovered (no-op when none),
        # so the next CLI walk skips the search calls; only the web flows
        # flushed before, leaving every CLI walk to re-resolve from cold.
        from qobuz_librarian.library.discovery import flush_resolve_cache
        flush_resolve_cache()
        _check_staging_occupied()


if __name__ == "__main__":
    _entry()
