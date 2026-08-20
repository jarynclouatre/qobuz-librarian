"""Interactive session mode picker."""
import textwrap

from qobuz_librarian.ui_cli.ask import ask
from qobuz_librarian.ui_cli.colors import C, fmt, text_width, wrap
from qobuz_librarian.ui_cli.logging import log
from qobuz_librarian.ui_cli.sentinels import Mode

# (key, name, mode, what it does). Held as data rather than pre-wrapped lines
# so the descriptions can be laid out to the terminal actually in front of the
# user; hand-wrapped at ~110 columns, they shredded on a phone.
_CHOICES = [
    ("1", "Search", Mode.ALBUM,
     "find and download one album (name or Qobuz URL)"),
    ("2", "Artist", Mode.ARTIST,
     "one artist: fill gaps, then offer albums you're missing"),
    ("3", "Library walk", Mode.WALK_QUEUE,
     "every artist, same as Artist; queue as you go and download after each "
     "artist or all at the end"),
    ("4", "Album gaps", Mode.ALBUM_WALK,
     "every incomplete album you own: fill missing tracks only, never "
     "suggests albums you don't have"),
    ("5", "Repair", Mode.ALBUM_REPAIR,
     "re-download damaged (truncated) tracks you own"),
    ("6", "Upgrade", Mode.UPGRADE,
     "better-quality versions of albums you own"),
    ("7", "Migrate", Mode.MIGRATE,
     "one-time setup: reorganise an existing library into the Artist/Album "
     "layout (copies by default, never touches originals)"),
    ("8", "Downsample", Mode.DOWNSAMPLE,
     "bring hi-res files you own down to CD quality to reclaim space"),
    ("9", "Lyrics", Mode.LYRICS,
     "fetch lyrics for tracks you own that are missing them"),
]

_BY_KEY = {key: mode for key, _name, mode, _desc in _CHOICES}


def _print_choices():
    """Two columns while they fit, name over description when they don't."""
    width = text_width()
    name_col = max(len(name) for _key, name, _mode, _desc in _CHOICES)
    head_len = len("    N) ") + name_col + len(": ")
    for key, name, _mode, desc in _CHOICES:
        if width - head_len < 32:
            log.info(fmt(C.WHITE, f"    {key}) {name}"))
            for line in textwrap.wrap(desc, max(20, width - 7)):
                log.info(fmt(C.GRAY, f"       {line}"))
            continue
        lines = textwrap.wrap(desc, width - head_len)
        log.info(fmt(C.WHITE, f"    {key}) {name.ljust(name_col)}: {lines[0]}"))
        for line in lines[1:]:
            log.info(fmt(C.GRAY, " " * head_len + line))


def interactive_session_mode():
    """Top-of-loop menu. Re-prompts on unrecognized input rather than
    falling through to album mode on a typo."""
    print()
    log.info(fmt(C.GRAY, wrap(
        "Qobuz Librarian: download Qobuz albums and fill your library's gaps.")))
    log.info(fmt(C.BOLD + C.CYAN, "  What would you like to do?"))
    _print_choices()
    log.info(fmt(C.WHITE, "    s) Settings"))
    log.info(fmt(C.WHITE, "    q) Quit"))
    while True:
        r = ask("  Choice (Enter = 1): ")
        if r is None:
            return Mode.QUIT
        if r in ("q", "quit", "exit"):
            return Mode.QUIT
        if r == "s":
            return Mode.SETTINGS
        if r == "":
            return Mode.ALBUM
        if r in _BY_KEY:
            return _BY_KEY[r]
        log.info(fmt(C.GRAY, "  Enter 1-9 (blank = 1), s for Settings, or q to quit."))
