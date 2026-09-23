"""Artist folders the last Library scan could not read.

A complete scan leaves them out rather than waiting on them. They have no saved
scan state, so once one can be read the next refresh checks it like a new
artist; this list is what says which folders were left out and when to look.
"""

from __future__ import annotations

import os
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file


def _path() -> Path:
    return Path(cfg.UNREADABLE_ARTISTS_FILE)


def record(names) -> bool:
    """Replace the list with this scan's unreadable folders; none clears it."""
    names = sorted({str(name) for name in names}, key=str.casefold)
    path = _path()
    if not names:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return True
    try:
        state_file.write_json(path, {"artists": names})
    except OSError:
        return False
    return True


def load() -> list[str]:
    try:
        data = state_file.load_json_object(
            _path(), "unreadable artist list", "the list of unreadable folders")
    except OSError:
        return []
    names = (data or {}).get("artists")
    if not isinstance(names, list):
        return []
    return [name for name in names if isinstance(name, str) and name]


def readable_again(names) -> list[str]:
    """The listed folders that can now be read end to end, or are gone."""
    back = []
    for name in names:
        folder = Path(cfg.MUSIC_ROOT) / name
        if not os.path.lexists(folder):
            back.append(name)
            continue
        errors = []
        for _root, _dirs, _files in os.walk(folder, onerror=errors.append):
            if errors:
                break
        if not errors:
            back.append(name)
    return back
