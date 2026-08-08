"""ANSI colour helpers."""
import os
import re
import shutil
import sys
import textwrap


class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    RED     = "\033[91m"
    BLUE    = "\033[94m"
    CYAN    = "\033[96m"
    GRAY    = "\033[90m"
    WHITE   = "\033[97m"
    MAGENTA = "\033[95m"


def _detect_color() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    _fc = os.environ.get("FORCE_COLOR", "")
    if _fc and _fc != "0":
        return True
    # die() and other error output goes to stderr, so honour a TTY on EITHER
    # stream - otherwise raw ANSI escapes leak into a redirected stdout while
    # the user watches a coloured terminal on stderr (or vice versa).
    return ((sys.stdout.isatty() or sys.stderr.isatty())
            and os.environ.get("TERM", "") != "dumb")


_enabled = _detect_color()


def set_color_enabled(enabled: bool):
    global _enabled
    _enabled = enabled


def fmt(color, text):
    if not _enabled:
        return str(text)
    return f"{color}{text}{C.RESET}"


def term_width(default=80):
    try:
        w = shutil.get_terminal_size((default, 24)).columns
        return max(40, w)
    except OSError:
        return default


# Text width every message shares: the real terminal, floored by term_width so
# a 20-column report doesn't wrap to one word a line, and capped so prose isn't
# strung across an ultrawide. The banner rule uses the same cap.
TEXT_CAP = 100


def text_width(cap=TEXT_CAP):
    return min(term_width(), cap)


def wrap(text, indent="  ", hanging=None, cap=TEXT_CAP):
    """One paragraph of prose reflowed to the terminal, `indent` on the first
    line and `hanging` on the rest.

    Pass the whole sentence - any line breaks already in it are collapsed
    first. Wrapping at write time is what makes the message fit a phone
    terminal; a string broken at authoring time is stuck at whatever width the
    author had. Colour goes on afterwards: fmt(C.RED, wrap(…)), not inside.
    """
    hanging = indent if hanging is None else hanging
    width = max(len(hanging) + 20, text_width(cap))
    lines = textwrap.wrap(" ".join(str(text).split()), width=width,
                          initial_indent=indent, subsequent_indent=hanging,
                          break_long_words=False, break_on_hyphens=False)
    return "\n".join(lines) if lines else indent.rstrip()


# A leading ✗/⚠/•/"1." that later lines of the same point should hang under.
_MARKER_RE = re.compile(r"^(?:[✗⚠✓⟳⤷·•]+|\d+\.)\s+")
# fmt()'s shape: one colour opened at the front, RESET at the very end.
_ONE_COLOR_RE = re.compile(r"^((?:\x1b\[[0-9;]*m)+)(.*)(\x1b\[0m)$", re.DOTALL)


def block(text, cap=TEXT_CAP):
    """A whole multi-line message reflowed to the terminal.

    Each line keeps its own indent, and one that opens with a ✗/⚠/• marker or a
    step number hangs its continuation under the text rather than under the
    marker - so headers, labelled lines and numbered steps stay aligned at any
    width. Blank lines are kept. Prose only: internal spacing is collapsed, so
    don't put an aligned listing through it.

    Accepts an already-coloured string as long as the colour covers the whole
    message (what fmt() produces); the colour is re-applied per line. A string
    coloured in pieces is returned untouched rather than risk breaking an
    escape sequence mid-word.
    """
    text = str(text)
    m = _ONE_COLOR_RE.match(text)
    if m:
        prefix, body, reset = m.groups()
        if "\x1b" not in body:
            return "\n".join(prefix + line + reset if line else line
                             for line in block(body, cap).split("\n"))
    if "\x1b" in text:
        return text
    out = []
    for line in text.split("\n"):
        body = line.lstrip(" ")
        if not body:
            out.append("")
            continue
        indent = line[: len(line) - len(body)]
        m = _MARKER_RE.match(body)
        if m is None:
            out.append(wrap(body, indent, indent, cap))
            continue
        # Keep the marker's own spacing (the ✗/⚠ headers carry two spaces).
        marker = m.group(0)
        out.append(wrap(body[len(marker):], indent + marker,
                        indent + " " * len(marker), cap))
    return "\n".join(out)


def truncate(s, n):
    s = str(s)
    if len(s) <= n:
        return s
    if n <= 1:
        return "…" if n == 1 else ""
    return s[: n - 1].rstrip() + "…"


def banner(title, color=None):
    # Goes through the shared logger so the same call site renders to
    # both the CLI stdout AND the web UI's captured SSE stream - using
    # bare print() here loses the banner in the web log.
    from qobuz_librarian.ui_cli.logging import log
    color = color or C.BLUE
    # Cap at 100: enough rule for a wide desktop terminal without it
    # looking ridiculous on an ultrawide; still fits a narrow ~60-col
    # window because term_width() never returns below 40.
    w = min(term_width(), 100)
    log.info("")
    log.info(fmt(C.BOLD + color, "═" * w))
    log.info(f"  {fmt(C.BOLD + C.WHITE, title)}")
    log.info(fmt(C.BOLD + color, "═" * w))


def section(title, color=None):
    from qobuz_librarian.ui_cli.logging import log
    color = color or C.BLUE
    log.info("")
    log.info(f"  {fmt(C.BOLD + color, '── ' + title + ' ──')}")


def format_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{int(n)}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"
