"""Album consolidation - find and remove duplicate sibling folders."""
import math
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path

from qobuz_librarian import beets_db, config
from qobuz_librarian.library import backup as backup_mod
from qobuz_librarian.library import catalog, scanner
from qobuz_librarian.library.scanner import parse_track_num
from qobuz_librarian.library.tags import (
    canonical_recording_id,
    canonical_track_slot,
    canonical_track_title,
    normalize,
    similarity,
    strip_album_decorations,
)
from qobuz_librarian.quality.decision import quality_change_summary
from qobuz_librarian.safefile import snapshot_tree, stat_identity
from qobuz_librarian.ui_cli.ask import ask
from qobuz_librarian.ui_cli.colors import C, fmt, section, truncate
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import log, vlog
from qobuz_librarian.ui_cli.prompts import (
    confirm,
    print_consolidation_overview,
    print_per_track_consolidation,
)


class ConsolidationCancelled(Exception):
    """Ctrl-C at a consolidation prompt, after ``moved`` tracks went to the
    backup folder."""

    def __init__(self, moved):
        super().__init__(moved)
        self.moved = moved


_CHANGED_SINCE_REVIEW = (
    "a track or its library entry changed after the review. Run the "
    "consolidation again to review them as they are now")
_YEAR_RE = re.compile(r"(?<!\d)(19\d\d|20\d\d)(?!\d)")


def _metadata_from_held_file(descriptor, path):
    parsed = None
    if scanner.HAVE_MUTAGEN:
        try:
            with os.fdopen(os.dup(descriptor), "rb") as source:
                parsed = scanner.mutagen.File(source, easy=True)
        except OSError:
            raise
        except Exception:
            parsed = None

    if parsed is not None and parsed.tags:
        tags = parsed.tags

        def first(key):
            value = tags.get(key)
            return value[0] if value and isinstance(value, list) else ""

        title = first("title")
        if title:
            info = parsed.info
            result = {
                "title": title,
                "isrc": first("isrc").strip().replace("-", "").upper(),
                "mb_trackid": first("musicbrainz_trackid").strip().lower(),
                "album": first("album"),
                "albumartist": first("albumartist") or first("artist"),
                "tracknumber": parse_track_num(first("tracknumber")),
                "discnumber": parse_track_num(first("discnumber")) or 1,
                "bits": getattr(info, "bits_per_sample", 0) if info else 0,
                "sample_rate": getattr(info, "sample_rate", 0) if info else 0,
                "channels": getattr(info, "channels", 0) if info else 0,
                "length": getattr(info, "length", 0.0) if info else 0.0,
                "path": str(path),
            }
            result["size"] = int(os.fstat(descriptor).st_size)
            result["normalized"] = normalize(title)
            return result

    match = re.match(r"^(\d+)[\s.\-]+(.+)$", path.stem)
    disc = re.match(r"(?:disc|cd)\s*0*(\d+)", path.parent.name, re.IGNORECASE)
    title = match.group(2) if match else path.stem
    return {
        "title": title,
        "tracknumber": int(match.group(1)) if match else 0,
        "isrc": "",
        "mb_trackid": "",
        "album": "",
        "albumartist": "",
        "discnumber": int(disc.group(1)) if disc else 1,
        "bits": 0,
        "sample_rate": 0,
        "channels": 0,
        "length": 0.0,
        "path": str(path),
        "size": int(os.fstat(descriptor).st_size),
        "normalized": normalize(title),
    }


@dataclass
class _ReviewedAlbum:
    path: Path
    identity: tuple[int, int]
    snapshot: dict
    tracks: list[dict]
    track_keys: frozenset[str]

    def matches(self, *, removed=()):
        """Whether the folder still holds what was reviewed, less `removed`.

        Folders are compared without their times, which move whenever a
        file in them is removed."""
        try:
            folder = os.lstat(self.path)
        except OSError:
            return False
        current = snapshot_tree(self.path)
        if (folder.st_dev, folder.st_ino) != self.identity or current is None:
            return False
        expected = {
            key: value for key, value in self.snapshot.items()
            if key not in removed
        }
        if current.keys() != expected.keys():
            return False
        return all(
            current[key][:3] == value[:3] if stat.S_ISDIR(value[2])
            else current[key] == value
            for key, value in expected.items()
        )

    def track_key(self, path):
        key = os.path.relpath(os.path.abspath(os.fspath(path)), self.path)
        return key if key in self.track_keys else None


def _review_album(path):
    path = Path(os.path.abspath(os.fspath(path)))
    if backup_mod._library_path(path) is None:
        raise OSError("album is not a real folder inside the music library")
    value = os.lstat(path)
    snapshot = snapshot_tree(path)
    if snapshot is None:
        raise OSError("album folder could not be read")
    audio_exts = set(config.AUDIO_EXTS)
    tracks = []
    track_keys = set()
    for key in sorted(snapshot, key=lambda key: key.split(os.sep)):
        identity = snapshot[key]
        if stat.S_ISDIR(identity[2]):
            continue
        track_path = path / key
        if not stat.S_ISREG(identity[2]):
            raise OSError(
                f"unsupported album entry prevents safe consolidation: {track_path}")
        if track_path.suffix.lower() not in audio_exts:
            continue
        descriptor = os.open(
            track_path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        )
        try:
            if stat_identity(os.fstat(descriptor)) != identity:
                raise OSError(
                    f"audio file changed while it was opened: {track_path}")
            metadata = _metadata_from_held_file(descriptor, track_path)
            if stat_identity(os.fstat(descriptor)) != identity:
                raise OSError(
                    f"audio file changed while metadata was read: {track_path}")
        finally:
            os.close(descriptor)
        tracks.append(metadata)
        track_keys.add(key)
    album = _ReviewedAlbum(
        path,
        (value.st_dev, value.st_ino),
        snapshot,
        tracks,
        frozenset(track_keys),
    )
    if not album.matches():
        raise OSError("album contents changed while they were reviewed")
    return album


def _file_receipt(identity):
    """The receipt the backup checks a file against, from its stat_identity."""
    return {
        "type": stat.S_IFMT(identity[2]),
        "device": identity[0],
        "inode": identity[1],
        "size": identity[3],
        "mtime_ns": identity[4],
        "ctime_ns": identity[5],
    }


def _sql_identifier(value):
    return '"' + str(value).replace('"', '""') + '"'


def _row_receipt(row):
    return tuple((type(value), value) for value in row)


def _database_schema(connection):
    known_tables = {
        "albums",
        "items",
        "album_attributes",
        "item_attributes",
        "migrations",
    }
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }
    core_tables = {
        "albums",
        "items",
        "album_attributes",
        "item_attributes",
    }
    if not core_tables.issubset(tables):
        raise sqlite3.DatabaseError("unrecognised beets database schema")
    if not tables.issubset(known_tables):
        raise sqlite3.DatabaseError(
            "unknown tables make exact track removal unverifiable")
    for table in tables:
        if connection.execute(
                f"PRAGMA foreign_key_list({_sql_identifier(table)})").fetchone():
            raise sqlite3.DatabaseError(
                "foreign keys make exact track removal unverifiable")
    if connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
        "AND tbl_name IN ("
        "'albums', 'items', 'album_attributes', 'item_attributes') LIMIT 1"
    ).fetchone():
        raise sqlite3.DatabaseError(
            "custom triggers make exact track removal unverifiable")
    item_info = tuple(connection.execute("PRAGMA table_info(items)"))
    attribute_info = tuple(connection.execute(
        "PRAGMA table_info(item_attributes)"))
    album_info = tuple(connection.execute("PRAGMA table_info(albums)"))
    album_attribute_info = tuple(connection.execute(
        "PRAGMA table_info(album_attributes)"))
    item_columns = tuple(row[1] for row in item_info)
    attribute_columns = tuple(row[1] for row in attribute_info)
    album_columns = tuple(row[1] for row in album_info)
    album_attribute_columns = tuple(row[1] for row in album_attribute_info)
    if (
        not {"id", "path", "album_id"}.issubset(item_columns)
        or not {"id", "entity_id", "key", "value"}.issubset(
            attribute_columns)
        or "id" not in album_columns
        or not {"id", "entity_id", "key", "value"}.issubset(
            album_attribute_columns)
    ):
        raise sqlite3.DatabaseError("unrecognised beets database schema")
    item_id = item_info[item_columns.index("id")]
    attribute_id = attribute_info[attribute_columns.index("id")]
    album_id = album_info[album_columns.index("id")]
    album_attribute_id = album_attribute_info[
        album_attribute_columns.index("id")]
    if (
        item_id[5] != 1
        or attribute_id[5] != 1
        or album_id[5] != 1
        or album_attribute_id[5] != 1
        or sum(bool(row[5]) for row in item_info) != 1
        or sum(bool(row[5]) for row in attribute_info) != 1
        or sum(bool(row[5]) for row in album_info) != 1
        or sum(bool(row[5]) for row in album_attribute_info) != 1
    ):
        raise sqlite3.DatabaseError("beets row identities are not unique")
    return (
        tuple(sorted(tables)),
        item_info,
        attribute_info,
        item_columns,
        attribute_columns,
        album_info,
        album_attribute_info,
        album_columns,
        album_attribute_columns,
    )


def _path_variants(paths):
    music_root = Path(os.path.abspath(os.fspath(config.MUSIC_ROOT)))
    variants = []
    for raw in paths:
        absolute = Path(os.path.abspath(os.fspath(raw)))
        try:
            relative = absolute.relative_to(music_root)
        except ValueError:
            raise OSError("consolidation track is outside the music library") from None
        for value in (
            str(absolute),
            os.fsencode(absolute),
            os.fspath(relative),
            os.fsencode(relative),
        ):
            if not any(type(value) is type(old) and value == old for old in variants):
                variants.append(value)
    return tuple(variants)


def _items_for_paths(connection, columns, variants):
    selected = ", ".join(_sql_identifier(column) for column in columns)
    query = (
        f"SELECT {selected} FROM items WHERE path = ? "
        f"ORDER BY {_sql_identifier('id')}"
    )
    rows = {}
    id_index = columns.index("id")
    for variant in variants:
        for row in connection.execute(query, (variant,)):
            row_id = row[id_index]
            if type(row_id) is not int:
                raise sqlite3.DatabaseError("beets item has an invalid identity")
            prior = rows.setdefault(row_id, row)
            if _row_receipt(prior) != _row_receipt(row):
                raise sqlite3.DatabaseError("beets item identity is duplicated")
    return tuple(rows[key] for key in sorted(rows))


def _items_for_ids(connection, columns, item_ids):
    if not item_ids:
        return ()
    placeholders = ",".join("?" for _ in item_ids)
    selected = ", ".join(_sql_identifier(column) for column in columns)
    rows = connection.execute(
        f"SELECT {selected} FROM items WHERE id IN ({placeholders}) "
        "ORDER BY id",
        tuple(item_ids),
    ).fetchall()
    id_index = columns.index("id")
    if any(type(row[id_index]) is not int for row in rows):
        raise sqlite3.DatabaseError("beets item has an invalid identity")
    if len({row[id_index] for row in rows}) != len(rows):
        raise sqlite3.DatabaseError("beets item identity is duplicated")
    return tuple(rows)


def _attributes_for_items(connection, columns, item_ids):
    if not item_ids:
        return ()
    placeholders = ",".join("?" for _ in item_ids)
    selected = ", ".join(_sql_identifier(column) for column in columns)
    rows = connection.execute(
        f"SELECT {selected} FROM item_attributes "
        f"WHERE entity_id IN ({placeholders}) ORDER BY id",
        tuple(item_ids),
    ).fetchall()
    id_index = columns.index("id")
    if any(type(row[id_index]) is not int for row in rows):
        raise sqlite3.DatabaseError("beets item attribute has an invalid identity")
    if len({row[id_index] for row in rows}) != len(rows):
        raise sqlite3.DatabaseError("beets item attribute identity is duplicated")
    return tuple(rows)


def _albums_for_ids(connection, columns, album_ids):
    if not album_ids:
        return ()
    placeholders = ",".join("?" for _ in album_ids)
    selected = ", ".join(_sql_identifier(column) for column in columns)
    rows = connection.execute(
        f"SELECT {selected} FROM albums WHERE id IN ({placeholders}) "
        "ORDER BY id",
        tuple(album_ids),
    ).fetchall()
    id_index = columns.index("id")
    if any(type(row[id_index]) is not int for row in rows):
        raise sqlite3.DatabaseError("beets album has an invalid identity")
    if len({row[id_index] for row in rows}) != len(rows):
        raise sqlite3.DatabaseError("beets album identity is duplicated")
    return tuple(rows)


def _items_for_albums(connection, columns, album_ids):
    if not album_ids:
        return ()
    placeholders = ",".join("?" for _ in album_ids)
    selected = ", ".join(_sql_identifier(column) for column in columns)
    rows = connection.execute(
        f"SELECT {selected} FROM items WHERE album_id IN ({placeholders}) "
        "ORDER BY id",
        tuple(album_ids),
    ).fetchall()
    id_index = columns.index("id")
    album_id_index = columns.index("album_id")
    expected = set(album_ids)
    if any(
        type(row[id_index]) is not int
        or type(row[album_id_index]) is not int
        or row[album_id_index] not in expected
        for row in rows
    ):
        raise sqlite3.DatabaseError("beets album membership is invalid")
    if len({row[id_index] for row in rows}) != len(rows):
        raise sqlite3.DatabaseError("beets item identity is duplicated")
    return tuple(rows)


def _attributes_for_albums(connection, columns, album_ids):
    if not album_ids:
        return ()
    placeholders = ",".join("?" for _ in album_ids)
    selected = ", ".join(_sql_identifier(column) for column in columns)
    rows = connection.execute(
        f"SELECT {selected} FROM album_attributes "
        f"WHERE entity_id IN ({placeholders}) ORDER BY id",
        tuple(album_ids),
    ).fetchall()
    id_index = columns.index("id")
    if any(type(row[id_index]) is not int for row in rows):
        raise sqlite3.DatabaseError("beets album attribute has an invalid identity")
    if len({row[id_index] for row in rows}) != len(rows):
        raise sqlite3.DatabaseError("beets album attribute identity is duplicated")
    return tuple(rows)


class _BeetsItemSeal:
    def __init__(
            self, database, schema_receipt=None,
            item_rows=(), attribute_rows=(), album_rows=(),
            album_item_rows=(), album_attribute_rows=(),
            empty_album_ids=()):
        self.database = database
        self.schema_receipt = schema_receipt
        self.item_rows = item_rows
        self.attribute_rows = attribute_rows
        self.album_rows = album_rows
        self.album_item_rows = album_item_rows
        self.album_attribute_rows = album_attribute_rows
        self.empty_album_ids = empty_album_ids
        self._closed = False

    @property
    def tracked(self):
        return len(self.item_rows)

    def _matches(self, connection, anchors_match):
        if (
            self._closed
            or not anchors_match()
            or _database_schema(connection) != self.schema_receipt
        ):
            return False
        item_columns = self.schema_receipt[3]
        attribute_columns = self.schema_receipt[4]
        album_columns = self.schema_receipt[7]
        album_attribute_columns = self.schema_receipt[8]
        item_ids = tuple(
            row[item_columns.index("id")] for row in self.item_rows)
        album_ids = tuple(
            row[album_columns.index("id")] for row in self.album_rows)
        current_items = _items_for_ids(
            connection, item_columns, item_ids)
        current_attributes = _attributes_for_items(
            connection, attribute_columns, item_ids)
        current_albums = _albums_for_ids(
            connection, album_columns, album_ids)
        current_album_items = _items_for_albums(
            connection, item_columns, album_ids)
        current_album_attributes = _attributes_for_albums(
            connection, album_attribute_columns, album_ids)
        return (
            tuple(map(_row_receipt, current_items))
            == tuple(map(_row_receipt, self.item_rows))
            and tuple(map(_row_receipt, current_attributes))
            == tuple(map(_row_receipt, self.attribute_rows))
            and tuple(map(_row_receipt, current_albums))
            == tuple(map(_row_receipt, self.album_rows))
            and tuple(map(_row_receipt, current_album_items))
            == tuple(map(_row_receipt, self.album_item_rows))
            and tuple(map(_row_receipt, current_album_attributes))
            == tuple(map(_row_receipt, self.album_attribute_rows))
        )

    def _delete_rows(self, connection):
        item_columns = self.schema_receipt[3]
        attribute_columns = self.schema_receipt[4]
        album_columns = self.schema_receipt[7]
        album_attribute_columns = self.schema_receipt[8]
        item_id_index = item_columns.index("id")
        attribute_id_index = attribute_columns.index("id")
        album_id_index = album_columns.index("id")
        album_attribute_id_index = album_attribute_columns.index("id")
        album_attribute_entity_index = album_attribute_columns.index("entity_id")
        for row in self.attribute_rows:
            deleted = connection.execute(
                "DELETE FROM item_attributes WHERE id = ?",
                (row[attribute_id_index],),
            )
            if deleted.rowcount != 1:
                raise sqlite3.IntegrityError(
                    "beets item attributes changed during consolidation")
        for row in self.item_rows:
            deleted = connection.execute(
                "DELETE FROM items WHERE id = ?", (row[item_id_index],))
            if deleted.rowcount != 1:
                raise sqlite3.IntegrityError(
                    "beets items changed during consolidation")
        empty_album_ids = set(self.empty_album_ids)
        for row in self.album_attribute_rows:
            if row[album_attribute_entity_index] not in empty_album_ids:
                continue
            deleted = connection.execute(
                "DELETE FROM album_attributes WHERE id = ?",
                (row[album_attribute_id_index],),
            )
            if deleted.rowcount != 1:
                raise sqlite3.IntegrityError(
                    "beets album attributes changed during consolidation")
        for row in self.album_rows:
            if row[album_id_index] not in empty_album_ids:
                continue
            deleted = connection.execute(
                "DELETE FROM albums WHERE id = ?", (row[album_id_index],))
            if deleted.rowcount != 1:
                raise sqlite3.IntegrityError(
                    "beets albums changed during consolidation")
        item_ids = tuple(row[item_id_index] for row in self.item_rows)
        if _items_for_ids(connection, item_columns, item_ids):
            raise sqlite3.IntegrityError(
                "beets item rows survived consolidation")
        if _attributes_for_items(connection, attribute_columns, item_ids):
            raise sqlite3.IntegrityError(
                "beets item attributes survived consolidation")
        if _albums_for_ids(
                connection, album_columns, self.empty_album_ids):
            raise sqlite3.IntegrityError(
                "empty beets album rows survived consolidation")
        if _attributes_for_albums(
                connection, album_attribute_columns, self.empty_album_ids):
            raise sqlite3.IntegrityError(
                "empty beets album attributes survived consolidation")

    def _deletions_match(self, connection, anchors_match):
        if (
            self._closed
            or not anchors_match()
            or _database_schema(connection) != self.schema_receipt
        ):
            return False
        item_columns = self.schema_receipt[3]
        attribute_columns = self.schema_receipt[4]
        album_columns = self.schema_receipt[7]
        album_attribute_columns = self.schema_receipt[8]
        item_id_index = item_columns.index("id")
        album_id_index = album_columns.index("id")
        album_attribute_entity_index = album_attribute_columns.index("entity_id")
        removed_item_ids = {
            row[item_id_index] for row in self.item_rows
        }
        album_ids = tuple(
            row[album_id_index] for row in self.album_rows)
        expected_album_items = tuple(
            row for row in self.album_item_rows
            if row[item_id_index] not in removed_item_ids
        )
        empty_album_ids = set(self.empty_album_ids)
        expected_albums = tuple(
            row for row in self.album_rows
            if row[album_id_index] not in empty_album_ids
        )
        expected_album_attributes = tuple(
            row for row in self.album_attribute_rows
            if row[album_attribute_entity_index] not in empty_album_ids
        )
        return (
            not _items_for_ids(
                connection, item_columns, tuple(removed_item_ids))
            and not _attributes_for_items(
                connection, attribute_columns, tuple(removed_item_ids))
            and tuple(map(_row_receipt, _items_for_albums(
                connection, item_columns, album_ids)))
            == tuple(map(_row_receipt, expected_album_items))
            and tuple(map(_row_receipt, _albums_for_ids(
                connection, album_columns, album_ids)))
            == tuple(map(_row_receipt, expected_albums))
            and tuple(map(_row_receipt, _attributes_for_albums(
                connection, album_attribute_columns, album_ids)))
            == tuple(map(_row_receipt, expected_album_attributes))
        )

    def run(self, mutate, before_matches, after_matches):
        if self._closed:
            return False, None, "the reviewed database binding was closed"
        if self.database is None:
            if not _no_database() or not before_matches():
                return False, None, _CHANGED_SINCE_REVIEW
            result = mutate()
            if result is None:
                return False, None, "the protected backup could not be made"
            if not _no_database() or not after_matches():
                return False, result, "the reviewed filesystem state changed"
            return True, result, None

        result = None
        try:
            with beets_db.transaction(self.database) as connection:
                if not self._matches(connection, before_matches):
                    raise OSError(_CHANGED_SINCE_REVIEW)
                # The recoverable copy must exist before database rows are
                # removed.
                result = mutate()
                if result is None:
                    connection.rollback()
                    return False, None, "the protected backup could not be made"
                if not self._matches(connection, after_matches):
                    raise OSError("the reviewed state changed during backup")
                self._delete_rows(connection)
                if not self._deletions_match(connection, after_matches):
                    raise OSError("the reviewed state changed before database commit")
        except Exception as exc:
            return False, result, str(exc)
        return True, result, None

    def close(self):
        self._closed = True


def _no_database():
    database = beets_db.path()
    return database is None or not database.is_file()


def _capture_beets_items(paths, anchors_match):
    if _no_database():
        if not anchors_match():
            raise OSError("reviewed album changed before database binding")
        return _BeetsItemSeal(None)
    database = beets_db.path()

    def capture(connection):
        schema_receipt = _database_schema(connection)
        variants = _path_variants(paths)
        item_rows = _items_for_paths(
            connection, schema_receipt[3], variants)
        item_columns = schema_receipt[3]
        attribute_columns = schema_receipt[4]
        album_columns = schema_receipt[7]
        album_attribute_columns = schema_receipt[8]
        item_id_index = item_columns.index("id")
        item_album_index = item_columns.index("album_id")
        item_ids = tuple(row[item_id_index] for row in item_rows)
        attribute_rows = _attributes_for_items(
            connection, attribute_columns, item_ids)
        album_ids = set()
        for row in item_rows:
            album_id = row[item_album_index]
            if album_id is None:
                continue
            if type(album_id) is not int or album_id <= 0:
                raise sqlite3.DatabaseError(
                    "beets item has an invalid album identity")
            album_ids.add(album_id)
        album_ids = tuple(sorted(album_ids))
        album_rows = _albums_for_ids(
            connection, album_columns, album_ids)
        album_row_id_index = album_columns.index("id")
        if tuple(
            row[album_row_id_index] for row in album_rows
        ) != album_ids:
            raise sqlite3.DatabaseError(
                "beets item refers to an unrecognised album")
        album_item_rows = _items_for_albums(
            connection, item_columns, album_ids)
        selected_ids = set(item_ids)
        empty_album_ids = []
        for album_id in album_ids:
            member_ids = {
                row[item_id_index]
                for row in album_item_rows
                if row[item_album_index] == album_id
            }
            selected_members = {
                row[item_id_index]
                for row in item_rows
                if row[item_album_index] == album_id
            }
            if not member_ids or not selected_members.issubset(member_ids):
                raise sqlite3.DatabaseError(
                    "beets album membership could not be proved")
            if member_ids.issubset(selected_ids):
                empty_album_ids.append(album_id)
        album_attribute_rows = _attributes_for_albums(
            connection, album_attribute_columns, album_ids)
        return _BeetsItemSeal(
            database,
            schema_receipt,
            item_rows,
            attribute_rows,
            album_rows,
            album_item_rows,
            album_attribute_rows,
            tuple(empty_album_ids),
        )

    if not anchors_match():
        raise OSError("reviewed album changed before database binding")
    seal = beets_db.read(database, capture)
    if not anchors_match():
        raise OSError("reviewed album changed while its database rows were read")
    return seal


@dataclass
class _ConsolidationBinding:
    primary: _ReviewedAlbum
    sibling: _ReviewedAlbum
    keys: tuple[str, ...]
    database: _BeetsItemSeal
    kept: dict | None = None
    used: bool = False

    @property
    def paths(self):
        return [self.sibling.path / key for key in self.keys]

    @property
    def expected_receipts(self):
        return {
            key: _file_receipt(self.sibling.snapshot[key])
            for key in self.keys
        }

    def before_matches(self):
        return self.primary.matches() and self.sibling.matches()

    def after_matches(self):
        return (
            self.primary.matches()
            and self.sibling.matches(removed=set(self.keys))
        )

    def close(self):
        self.database.close()


def _bind_summary(summary, primary, sibling):
    keys = []
    # Which primary track stands in for each sibling file, so the backup
    # can expire once the primary still holds them.
    kept = {}
    for track, primary_track in summary["overlap"]:
        raw_path = track.get("path")
        key = sibling.track_key(raw_path) if raw_path else None
        if key is None or key in keys:
            raise OSError("overlap no longer maps to one reviewed audio file")
        keys.append(key)
        primary_path = primary_track.get("path")
        standing_in = primary.track_key(primary_path) if primary_path else None
        if kept is not None and standing_in is not None:
            kept[key] = standing_in
        else:
            kept = None
    if not keys:
        return summary
    anchors_match = lambda: primary.matches() and sibling.matches()
    database = _capture_beets_items(
        [sibling.path / key for key in keys], anchors_match)
    summary["_binding"] = _ConsolidationBinding(
        primary, sibling, tuple(keys), database, kept)
    return summary


def _years_in(name: str) -> set:
    """The set of plausible release years embedded in a folder name."""
    return set(_YEAR_RE.findall(name or ""))


def _same_recording_signal(a, b):
    """Positive 'same recording' evidence for two tracks that share a title but
    have no ISRC/MBID to confirm identity: their durations must agree within ~2s.

    When either side has no readable duration there is NO real evidence, so this
    returns False and the caller keeps both copies. A title (and even a track
    slot) can coincide between two genuinely different recordings - file size is
    too weak a tiebreak to risk an irreversible delete on, so it is not used."""
    la, lb = a.get("length"), b.get("length")
    if isinstance(la, bool) or isinstance(lb, bool):
        return False
    try:
        la, lb = float(la or 0), float(lb or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    return (
        la > 0 and lb > 0
        and math.isfinite(la) and math.isfinite(lb)
        and abs(la - lb) <= 2.0
    )


def match_sibling_track(sibling_track, primary_tracks):
    """Match sibling → primary by ISRC > MBID > (disc, title + same recording)."""
    s_isrc = canonical_recording_id(sibling_track.get("isrc"), "isrc")
    s_mbid = canonical_recording_id(
        sibling_track.get("mb_trackid"), "mb_trackid")
    if s_isrc is None or s_mbid is None:
        return None
    s_title_norm = canonical_track_title(sibling_track.get("title"))
    s_slot = canonical_track_slot(
        sibling_track, "discnumber", "tracknumber")

    if s_isrc:
        matches = []
        for pt in primary_tracks:
            primary_isrc = canonical_recording_id(pt.get("isrc"), "isrc")
            primary_mbid = canonical_recording_id(
                pt.get("mb_trackid"), "mb_trackid")
            if (
                primary_isrc is None
                or primary_mbid is None
                or primary_isrc != s_isrc
            ):
                continue
            if s_mbid and primary_mbid and s_mbid != primary_mbid:
                continue
            matches.append(pt)
        # A one-sided recording ID is uncertainty, not permission to fall back
        # to a title before deleting the sibling's file.
        return matches[0] if len(matches) == 1 else None
    if s_mbid:
        matches = []
        for pt in primary_tracks:
            primary_isrc = canonical_recording_id(pt.get("isrc"), "isrc")
            primary_mbid = canonical_recording_id(
                pt.get("mb_trackid"), "mb_trackid")
            if (
                primary_isrc is None
                or primary_mbid is None
                or primary_isrc
                or primary_mbid != s_mbid
            ):
                continue
            matches.append(pt)
        return matches[0] if len(matches) == 1 else None
    if s_title_norm and s_slot is not None:
        if sum(
            canonical_track_slot(pt, "discnumber", "tracknumber") == s_slot
            for pt in primary_tracks
        ) != 1:
            return None
        # Title + disc + position is NOT recording identity without an
        # ISRC/MBID: a live setlist replayed on another date (or any two
        # distinct same-titled recordings sharing a slot) matches all three.
        matches = []
        for pt in primary_tracks:
            primary_isrc = canonical_recording_id(pt.get("isrc"), "isrc")
            primary_mbid = canonical_recording_id(
                pt.get("mb_trackid"), "mb_trackid")
            if (
                primary_isrc is None
                or primary_mbid is None
                or primary_isrc
                or primary_mbid
                or canonical_track_title(pt.get("title")) != s_title_norm
                or canonical_track_slot(
                    pt, "discnumber", "tracknumber") != s_slot
            ):
                continue
            if _same_recording_signal(sibling_track, pt):
                matches.append(pt)
        return matches[0] if len(matches) == 1 else None
    return None


def _summary_from_tracks(sibling, score, sibling_tracks, primary_tracks):
    overlap, unique = [], []
    claimed = set()
    for track in sibling_tracks:
        match = match_sibling_track(track, primary_tracks)
        if match is not None and id(match) not in claimed:
            claimed.add(id(match))
            overlap.append((track, match))
        else:
            unique.append(track)
    return {
        "dir": sibling,
        "score": score,
        "all_tracks": sibling_tracks,
        "overlap": overlap,
        "unique": unique,
    }


def _reviewed_sibling_albums(album, primary):
    primary_bare = strip_album_decorations(primary.path.name)
    primary_years = (
        _years_in(primary.path.name)
        or _years_in(album.get("title") or "")
    )
    api_bare = strip_album_decorations(album.get("title") or "")
    artist = primary.path.parent
    try:
        names = sorted(os.listdir(artist))
    except OSError as exc:
        raise OSError("couldn't list the artist folder") from exc
    candidates = []
    for name in names:
        try:
            value = os.lstat(artist / name)
        except OSError:
            continue
        if (
            not stat.S_ISDIR(value.st_mode)
            or (value.st_dev, value.st_ino) == primary.identity
        ):
            continue
        years = _years_in(name)
        if primary_years and years and primary_years.isdisjoint(years):
            continue
        bare = strip_album_decorations(name)
        score = max(
            similarity(bare, primary_bare),
            similarity(bare, api_bare) if api_bare else 0.0,
        )
        if score < config.CONSOLIDATE_THRESH:
            continue
        try:
            sibling = _review_album(artist / name)
        except OSError as exc:
            log.info(fmt(
                C.YELLOW,
                f"  ⚠  Couldn't safely inspect {name} ({exc}); left it untouched.",
            ))
            continue
        candidates.append((sibling, score))
    return sorted(candidates, key=lambda item: -item[1])


def _remove_empty_folders(album):
    """rmdir each folder of the album, deepest first, that is empty and is
    still the folder reviewed, so a link put in the path is not followed."""
    folders = [
        (album.path / key, album.snapshot[key][:3])
        for key in sorted(
            (key for key, value in album.snapshot.items()
             if stat.S_ISDIR(value[2])),
            key=lambda key: (-key.count(os.sep), key.split(os.sep)),
        )
    ]
    for folder, expected in (*folders, (album.path, album.identity)):
        try:
            value = os.lstat(folder)
            if (
                not stat.S_ISDIR(value.st_mode)
                or stat_identity(value)[:len(expected)] != expected
                or os.listdir(folder)
            ):
                continue
        except OSError:
            continue
        try:
            os.rmdir(folder)
        except OSError:
            log.info(fmt(
                C.YELLOW,
                f"    ⚠  Couldn't remove the empty folder "
                f"{folder.name}; left it in place.",
            ))
            continue
        log.info(fmt(
            C.GREEN,
            f"    ✓  Removed empty folder: {folder.name}.",
        ))


def _carry_left_lyrics(binding):
    """Give each kept track the lyrics of the .lrc its set-aside duplicate
    left behind, by the Lyrics pass's rule, so they are not only in a folder
    without its tracks."""
    from qobuz_librarian.integrations import lyric_fetch

    for key, standing_in in (binding.kept or {}).items():
        sidecar = (binding.sibling.path / key).with_suffix(".lrc")
        if Path(standing_in).suffix.lower() != ".flac":
            continue
        try:
            if not stat.S_ISREG(os.lstat(sidecar).st_mode):
                continue
            lyric_fetch.give_lyrics(
                binding.primary.path / standing_in, config.MUSIC_ROOT,
                sidecar.read_text(encoding="utf-8", errors="replace"),
                config.LYRICS_FORMAT)
        except FileNotFoundError:
            continue
        except Exception as exc:
            log.info(fmt(
                C.YELLOW,
                f"    ⚠  Couldn't copy the lyrics in {sidecar.name} onto the "
                f"kept album ({exc}); the file stays in "
                f"{binding.sibling.path.name}.",
            ))


def execute_consolidation(summary):
    """Move the overlapping sibling tracks to a recoverable backup instead of
    hard-deleting them, then return (removed_paths, n_failed) so the caller can
    drop exactly those files from the beets DB.

    Consolidation is the one destructive mode whose duplicate match can rest on a
    title+disc+(track)+duration-within-2s heuristic when neither side carries an
    ISRC/MBID, so a mistaken match would otherwise unlink a genuinely different
    recording with no way back. The backup keeps them restorable for the
    retention window; the age sweep then clears it only while the primary
    still holds a decode-clean track at least as long for each of them."""
    binding = summary.get("_binding")
    attempted = len(summary.get("overlap") or ())
    if not isinstance(binding, _ConsolidationBinding) or binding.used:
        log.info(fmt(
            C.RED,
            "      ✗  consolidation stopped: the reviewed files were not "
            "bound to an exact transaction.",
        ))
        return [], attempted
    binding.used = True
    paths = binding.paths

    def move_to_backup():
        return backup_mod.backup_gap_fill_files(
            paths,
            binding.sibling.path,
            expected_receipts=binding.expected_receipts,
        )

    success, backup_result, reason = binding.database.run(
        move_to_backup,
        binding.before_matches,
        binding.after_matches,
    )
    if not success:
        if backup_result is not None:
            scanner.clear_scan_caches()
            log.info(fmt(
                C.RED,
                "      ✗  consolidation did not reach a fully verified "
                "commit; its protected copy is retained at "
                f"{backup_result.path}.",
            ))
            log.info(fmt(
                C.YELLOW,
                "         The live track names and Beets rows may now "
                "disagree; restore from that retained backup before retrying.",
            ))
            if reason:
                log.info(fmt(C.GRAY, f"         {reason}."))
        elif reason == _CHANGED_SINCE_REVIEW:
            log.info(fmt(C.RED, f"      ✗  Nothing was moved: {reason}."))
        else:
            log.info(fmt(
                C.RED, f"      ✗  Consolidation stopped: {reason or 'an unexpected error'}."))
        return [], attempted

    if binding.database.tracked:
        log.info(fmt(C.GRAY, "    ⤷  Updated the beets library."))
    if backup_result is not None:
        expires = binding.kept is not None and backup_mod.mark_consolidated(
            backup_result, binding.primary.path, binding.kept)
        log.info(fmt(C.GRAY, f"    ⤷  Backup folder: {backup_result.path}"
                     + (f" (cleared after "
                        f"{plural(config.UPGRADE_BACKUP_RETENTION_DAYS, 'day')})"
                        if expires else "")))
    _carry_left_lyrics(binding)
    scanner.clear_scan_caches()
    _remove_empty_folders(binding.sibling)
    for path in paths:
        vlog(f"consolidation: moved {path} to protected backup")
    return paths, 0


def consolidate_albums(album, args):
    """Top-level consolidation flow. Always interactive - --yes does NOT silence it.

    Returns how many tracks moved to the backup folder and how many were not."""
    section("Consolidate similar album folders")
    # Consolidation deletes overlapping sibling tracks, so it must never run
    # under --dry-run - including on the "already complete" album path, which
    # reaches here before process_album's own dry-run stop.
    if getattr(args, "dry_run", False):
        log.info(fmt(C.GRAY,
            "  --dry-run: skipping consolidation (it would delete overlapping tracks)."))
        return 0, 0

    primary_dir = catalog.find_album_dir_filesystem(album)
    if primary_dir is None:
        log.info(fmt(C.YELLOW, "  ⚠  Couldn't locate primary album folder after import."))
        log.info(fmt(C.GRAY,   "     Skipping consolidation."))
        return 0, 0
    summaries = []
    n_actioned = 0
    n_failed = 0
    moving = False
    try:
        try:
            primary = _review_album(primary_dir)
        except OSError as exc:
            log.info(fmt(
                C.YELLOW,
                f"  ⚠  Couldn't safely inspect the primary album ({exc}).",
            ))
            log.info(fmt(C.GRAY, "     Skipping consolidation."))
            return 0, 0
        log.info(fmt(C.GRAY, f"  Primary: {primary.path}"))
        if not primary.tracks:
            log.info(fmt(
                C.YELLOW,
                "  ⚠  No tracks read from primary folder. Skipping consolidation.",
            ))
            return 0, 0

        try:
            candidates = _reviewed_sibling_albums(album, primary)
        except OSError as exc:
            log.info(fmt(C.YELLOW, f"  ⚠  Consolidation stopped ({exc})."))
            return 0, 0
        if not candidates:
            log.info(fmt(
                C.GREEN,
                "  ✓  No sibling album folders found. Nothing to consolidate.",
            ))
            return 0, 0

        for sibling, score in candidates:
            summary = _summary_from_tracks(
                sibling.path,
                score,
                sibling.tracks,
                primary.tracks,
            )
            if summary["overlap"]:
                try:
                    _bind_summary(summary, primary, sibling)
                except (OSError, sqlite3.Error) as exc:
                    summary["_blocked"] = str(exc)
                    log.info(fmt(
                        C.YELLOW,
                        f"  ⚠  Couldn't bind {sibling.path.name} to an exact "
                        f"filesystem/database transaction ({exc}); it will be "
                        "left untouched.",
                    ))
            summaries.append(summary)

        print_consolidation_overview(summaries)
        print()
        log.info(fmt(C.BOLD + C.CYAN, "  Per-sibling actions:"))

        for summary in summaries:
            sibling_dir = summary["dir"]
            overlap_count = len(summary["overlap"])
            if not overlap_count:
                continue
            if summary.get("_blocked"):
                log.info(fmt(
                    C.YELLOW,
                    f"  ⚠  {sibling_dir.name} was left untouched because its "
                    "review state could not be held safely.",
                ))
                continue

            quality = quality_change_summary(summary["overlap"])
            risky = quality["losing_hires"] + quality["unknown"]
            warning = risky > 0

            print()
            log.info(fmt(
                C.BOLD + C.WHITE,
                f"  Sibling: {truncate(sibling_dir.name, 55)}",
            ))
            log.info(fmt(
                C.GRAY,
                f"    {plural(overlap_count, 'track')} "
                f"{'overlaps' if overlap_count == 1 else 'overlap'} • "
                f"{plural(len(summary['unique']), 'bonus track')} will remain",
            ))
            if warning:
                if quality["unknown"]:
                    log.info(fmt(
                        C.RED + C.BOLD,
                        f"    ⚠  {plural(risky, 'track')} here could lose quality "
                        f"({quality['losing_hires']} higher than primary, "
                        f"{quality['unknown']} uncertain or different layout)!",
                    ))
                else:
                    log.info(fmt(
                        C.RED + C.BOLD,
                        f"    ⚠  {plural(quality['losing_hires'], 'track')} here "
                        f"{'is' if quality['losing_hires'] == 1 else 'are'} "
                        "HIGHER quality than primary!",
                    ))
                print_per_track_consolidation(summary)

            log.info(fmt(C.WHITE, "    [d] move overlapping tracks to the backup "
                                  "folder (default)"))
            log.info(fmt(C.WHITE, "    [s] show per-track details"))
            log.info(fmt(C.WHITE, "    [k] keep this sibling untouched"))
            log.info(fmt(C.WHITE, "    [q] stop consolidation entirely"))

            while True:
                choice = ask("    Choice [d]: ")
                if choice is None:
                    choice = "k"
                choice = choice or "d"
                if choice == "s":
                    print_per_track_consolidation(summary)
                    continue
                if choice == "k":
                    log.info(fmt(C.GRAY, "    Skipped."))
                    break
                if choice == "q":
                    log.info(fmt(C.GRAY, "    Stopping consolidation."))
                    return n_actioned, n_failed
                if choice == "d":
                    if warning:
                        log.info(fmt(
                            C.YELLOW + C.BOLD,
                            "    Type 'DELETE' to confirm a possible quality "
                            f"loss on {plural(risky, 'track')}:",
                        ))
                        typed = ask("    > ", lower=False)
                        if typed != "DELETE":
                            log.info(fmt(
                                C.GRAY, "    Confirmation not given. Skipped."))
                            break
                    elif not confirm(
                        f"    Move {plural(overlap_count, 'track')} from this sibling "
                        "to the backup folder?",
                        default_yes=True,
                        auto_yes=False,
                    ):
                        log.info(fmt(C.GRAY, "    Skipped."))
                        break
                    moving = True
                    deleted, failures = execute_consolidation(summary)
                    deleted_count = len(deleted)
                    n_actioned += deleted_count
                    n_failed += failures
                    moving = False
                    if deleted_count:
                        log.info(fmt(
                            C.GREEN,
                            f"    ✓  Moved {plural(deleted_count, 'track')} "
                            "to the backup folder.",
                        ))
                    if failures:
                        log.info(fmt(
                            C.RED,
                            f"    ✗  {plural(failures, 'track')} not "
                            "consolidated; see above.",
                        ))
                    break
                log.info(fmt(C.GRAY, "    Enter d / s / k / q."))

        return n_actioned, n_failed
    except KeyboardInterrupt:
        if moving:
            raise
        log.info(fmt(C.GRAY, "\n  Consolidation cancelled. " + (
            f"{plural(n_actioned, 'track')} had already moved to the backup "
            "folder." if n_actioned else "Nothing changed.")))
        raise ConsolidationCancelled(n_actioned) from None
    finally:
        for summary in summaries:
            binding = summary.get("_binding")
            if isinstance(binding, _ConsolidationBinding):
                binding.close()
