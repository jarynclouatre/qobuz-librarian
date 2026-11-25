"""Update indexed audio properties after an in-place Downsample or Restore."""
import errno
import json
import os
import secrets
import sqlite3
import stat
from pathlib import Path

from mutagen import MutagenError
from mutagen.flac import FLAC

from qobuz_librarian import beets_db
from qobuz_librarian import config as cfg
from qobuz_librarian.integrations.lyric_fetch import _BoundTrack
from qobuz_librarian.safefile import write_json
from qobuz_librarian.ui_cli.logging import log

_PREFIX = ".qobuz-downsample-index-"


def before_rewrite(paths):
    """Record which library files may change before their atomic replacements."""
    database = beets_db.path()
    if database is None:
        return None
    try:
        database.stat()
    except FileNotFoundError:
        return None
    except OSError:
        pass  # An unavailable existing index can still be refreshed on restart.
    root = Path(cfg.MUSIC_ROOT)
    staging = Path(cfg.STAGING_DIR)
    originals = {}
    for path in paths:
        if (not path.is_relative_to(root) or path.is_relative_to(staging)
                or path.suffix.lower() != ".flac"):
            continue
        try:
            source = path.lstat()
        except FileNotFoundError:
            source = None
        if source is not None and not stat.S_ISREG(source.st_mode):
            continue
        originals[str(path.relative_to(root))] = (
            [source.st_dev, source.st_ino] if source else None)
    if not originals:
        return None
    note = Path(cfg.DATA_DIR) / (_PREFIX + secrets.token_hex(12) + ".json")
    write_json(note, originals, strict=True)
    return note


def _refresh(connection, originals):
    root = Path(cfg.MUSIC_ROOT)
    paths = {}
    for name in originals:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() != ".flac":
            raise ValueError("invalid track path")
        paths[os.fsencode(relative)] = name
        paths[os.fsencode(root / relative)] = name
    # Beets stores paths as bytes, either absolute or relative to its library.
    keys = list(paths)
    for start in range(0, len(keys), 900):
        group = keys[start:start + 900]
        slots = ",".join("(?)" for _ in group)
        rows = connection.execute(
            f"WITH paths(p) AS (VALUES {slots}) "
            "SELECT id, CAST(path AS BLOB) FROM items WHERE path IN "
            "(SELECT p FROM paths UNION ALL SELECT CAST(p AS TEXT) FROM paths)",
            group).fetchall()
        for item_id, key in rows:
            name = paths[key]
            try:
                held = _BoundTrack(root / name, root)
            except OSError as exc:
                if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
                    continue
                raise
            with held:
                st = os.fstat(held.track_fd)
                if [st.st_dev, st.st_ino] == originals[name]:
                    continue
                with os.fdopen(os.dup(held.track_fd), "rb") as fh:
                    audio = FLAC(fh).info
                if not held.exact_track_is_named():
                    raise OSError("the track changed while reading its audio properties")
                bitrate = audio.bitrate
                if not bitrate and audio.length:
                    bitrate = int(st.st_size * 8 / audio.length)
                connection.execute(
                    "UPDATE items SET samplerate=?, bitdepth=?, bitrate=?, channels=?, length=? "
                    "WHERE id=?", (audio.sample_rate, audio.bits_per_sample, bitrate,
                                   audio.channels, audio.length, item_id))


def finish(note):
    if note is None:
        return
    try:
        originals = json.loads(note.read_text(encoding="utf-8"))
        with beets_db.transaction(beets_db.path(), wait=False) as connection:
            _refresh(connection, originals)
        note.unlink()
    except (OSError, sqlite3.Error, MutagenError, TypeError, ValueError) as exc:
        log.warning("  Couldn't refresh audio properties in beets (%s). "
                    "The app will retry at startup.", exc)


def finish_pending():
    for note in sorted(Path(cfg.DATA_DIR).glob(_PREFIX + "*.json")):
        finish(note)
