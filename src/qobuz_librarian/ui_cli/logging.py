"""Logging setup and verbose-output helper.

All modules that need log or vlog import from here. _VERBOSE is a module-level
flag; set_verbose() is called by cli.py at startup based on --verbose arg.
"""
import logging
import re
import sys

from qobuz_librarian.ui_cli.colors import C, fmt

log = logging.getLogger("qobuz_librarian")
log.setLevel(logging.INFO)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter("%(message)s"))
# Pin the console to INFO so lowering the LOGGER to DEBUG for the file handler
# (see attach_file_handler) doesn't flood the terminal - DEBUG belongs in the
# log file, not on screen. set_quiet() raises this to WARNING for --quiet.
_sh.setLevel(logging.INFO)
if not log.handlers:
    log.addHandler(_sh)

# ANSI escape codes pollute the file log; strip before writing.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class _StripAnsiFormatter(logging.Formatter):
    def format(self, record):
        s = super().format(record)
        return _ANSI_RE.sub("", s)


_file_handler = None


def attach_file_handler(path, level_name: str = "INFO", role: str = ""):
    """Attach a rotating file handler at `path`. Idempotent - safe to call
    from both _entry() and the web _lifespan.

    ``role`` names the writing process (e.g. "cli"); when set, the handler
    writes to a role-suffixed file (qobuz-librarian-cli.log). The long-lived web
    server and a `docker exec` CLI run can both be attached to the SAME log at
    once, and a single shared RotatingFileHandler races on rollover at the 5 MB
    boundary - two processes independently rename .log->.log.1 and reshuffle the
    backup chain, losing lines and orphaning an inode. A distinct file per role
    sidesteps the race without a cross-process rollover lock."""
    global _file_handler
    if _file_handler is not None:
        return
    from logging.handlers import RotatingFileHandler
    from pathlib import Path
    p = Path(path)
    if role:
        p = p.with_name(f"{p.stem}-{role}{p.suffix}")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(p, maxBytes=5 * 1024 * 1024, backupCount=3,
                                encoding="utf-8")
        h.setFormatter(_StripAnsiFormatter(
            "%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
        level = getattr(logging, level_name.upper(), logging.INFO)
        h.setLevel(level)
        # The logger itself gates at INFO (line 13), which would drop DEBUG
        # records before any handler saw them - so LOG_LEVEL=DEBUG was a no-
        # op.
        log.setLevel(min(log.level, level))
        log.addHandler(h)
        _file_handler = h
    except OSError:
        # Logging is best-effort - don't crash startup if the data volume
        # isn't writable.
        pass

# Progress-reporting hook.
_progress_reporter = None


def set_progress_reporter(fn):
    global _progress_reporter
    _progress_reporter = fn


def report_progress(phase, current=0, total=0, item=""):
    if _progress_reporter is not None:
        try:
            _progress_reporter(phase, current, total, item)
        except Exception:
            pass


# Optional thread-context wrapper, injected by the web JobManager.
_thread_wrapper = None


def set_thread_wrapper(fn):
    global _thread_wrapper
    _thread_wrapper = fn


def wrap_thread_target(target):
    """Wrap a thread target so it inherits the spawning thread's job context.
    Call on the SPAWNING thread (it captures context at call time)."""
    return _thread_wrapper(target) if _thread_wrapper else target


_VERBOSE = False


def set_verbose(v: bool):
    global _VERBOSE
    _VERBOSE = v


def set_quiet(quiet: bool):
    """Mute info-level output on the console; warnings and errors still print.

    Raises the level on the stdout handler rather than on the logger, so the
    file log keeps recording at its own level - a quiet cron run still leaves
    a full trail to diagnose from. Non-quiet resets to INFO (not NOTSET) so the
    console stays at INFO even when LOG_LEVEL=DEBUG lowered the logger for the
    file handler."""
    _sh.setLevel(logging.WARNING if quiet else logging.INFO)


def vlog(msg):
    if _VERBOSE:
        log.info(fmt(C.GRAY, f"    {msg}"))
