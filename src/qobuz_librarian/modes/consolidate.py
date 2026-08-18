"""Album consolidation - find and remove duplicate sibling folders."""
import hashlib
import math
import os
import re
import sqlite3
import stat
from dataclasses import dataclass, field
from pathlib import Path

from qobuz_librarian import config
from qobuz_librarian.file_exclusion import acquire_inode_write_exclusion
from qobuz_librarian.integrations import beets as beets_integration
from qobuz_librarian.library import backup as backup_mod
from qobuz_librarian.library import catalog, scanner
from qobuz_librarian.library.scanner import parse_track_num
from qobuz_librarian.library.sqlite_atomic import (
    AtomicSQLiteWrite,
    inspect_sqlite_source,
)
from qobuz_librarian.library.tags import (
    canonical_recording_id,
    canonical_track_slot,
    canonical_track_title,
    normalize,
    similarity,
    strip_album_decorations,
)
from qobuz_librarian.quality.decision import quality_change_summary
from qobuz_librarian.ui_cli.ask import ask
from qobuz_librarian.ui_cli.colors import C, fmt, section, truncate
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import log, vlog
from qobuz_librarian.ui_cli.prompts import (
    confirm,
    print_consolidation_overview,
    print_per_track_consolidation,
)

_YEAR_RE = re.compile(r"(?<!\d)(19\d\d|20\d\d)(?!\d)")
_MAX_ALBUM_ENTRIES = 4096
_MAX_ALBUM_DEPTH = 16


def _digest_fd(descriptor):
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)
        offset += len(chunk)


def _file_receipt(descriptor):
    value = os.fstat(descriptor)
    if not stat.S_ISREG(value.st_mode):
        raise OSError("consolidation source is not a regular file")
    return {
        "type": stat.S_IFMT(value.st_mode),
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "size": int(value.st_size),
        "mtime_ns": int(value.st_mtime_ns),
        "ctime_ns": int(value.st_ctime_ns),
        "sha256": _digest_fd(descriptor),
    }


def _same_held_file(descriptor, expected, *, moved=False):
    try:
        current = _file_receipt(descriptor)
    except OSError:
        return False
    fields = ("type", "device", "inode", "size", "sha256") if moved else tuple(expected)
    return all(current.get(field) == expected.get(field) for field in fields)


def _named_file_matches(parent_fd, name, descriptor):
    try:
        held = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(held.st_mode)
        and stat.S_ISREG(named.st_mode)
        and (int(held.st_dev), int(held.st_ino))
        == (int(named.st_dev), int(named.st_ino))
    )


def _named_entry_missing(parent_fd, name):
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


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
class _HeldFile:
    path: Path
    relative: tuple[str, ...]
    name: str
    parent_fd: int
    descriptor: int
    receipt: dict
    metadata: dict | None
    lease: object | None

    def close(self):
        if self.lease is not None:
            self.lease.close()
            self.lease = None
        try:
            os.close(self.descriptor)
        except OSError:
            pass


@dataclass
class _HeldDirectory:
    path: Path
    relative: tuple[str, ...]
    name: str
    descriptor: int
    parent_fd: int
    files: list[_HeldFile] = field(default_factory=list)
    children: list["_HeldDirectory"] = field(default_factory=list)


class _AlbumSeal:
    def __init__(self, path, root_chain, root_names, album_chain, album_parts):
        self.path = path
        self.root_chain = root_chain
        self.root_names = root_names
        self.album_chain = album_chain
        self.album_parts = album_parts
        self.root = _HeldDirectory(
            path,
            (),
            album_parts[-1],
            album_chain[-1],
            album_chain[-2],
        )
        self.files: list[_HeldFile] = []
        self.tracks: list[dict] = []
        self._closed = False

    def _directory_matches(self, directory, removed, cleaned):
        if directory.relative in cleaned:
            try:
                return os.fstat(directory.descriptor).st_nlink == 0
            except OSError:
                return False
        if not beets_integration._merge_directory_matches(
                directory.descriptor, directory.parent_fd, directory.name):
            return False
        expected_names = {
            item.name for item in directory.files
            if item.relative not in removed
        }
        expected_names.update(
            child.name for child in directory.children
            if child.relative not in cleaned
        )
        try:
            if set(os.listdir(directory.descriptor)) != expected_names:
                return False
        except OSError:
            return False
        for item in directory.files:
            if item.lease is not None and not item.lease.intact():
                return False
            if item.relative in removed:
                if (
                    not _named_entry_missing(directory.descriptor, item.name)
                    or not _same_held_file(
                        item.descriptor, item.receipt, moved=True)
                ):
                    return False
            elif (
                not _named_file_matches(
                    directory.descriptor, item.name, item.descriptor)
                or not _same_held_file(item.descriptor, item.receipt)
            ):
                return False
        return all(
            self._directory_matches(child, removed, cleaned)
            for child in directory.children
        )

    def matches(self, *, removed=(), cleaned=()):
        if self._closed:
            return False
        removed = set(removed)
        cleaned = set(cleaned)
        return (
            beets_integration._merge_chain_is_named(
                self.root_chain, self.root_names)
            and beets_integration._merge_chain_is_named(
                self.album_chain, self.album_parts)
            and self._directory_matches(self.root, removed, cleaned)
        )

    def held_file(self, path):
        absolute = Path(os.path.abspath(os.fspath(path)))
        return next((item for item in self.files if item.path == absolute), None)

    def remove_empty_directories(self, removed):
        removed = {item.relative for item in removed}
        cleaned = set()
        directories = []

        def collect(directory):
            directories.append(directory)
            for child in directory.children:
                collect(child)

        collect(self.root)
        for directory in sorted(
                directories, key=lambda item: len(item.relative), reverse=True):
            if not self.matches(removed=removed, cleaned=cleaned):
                log.info(fmt(
                    C.YELLOW,
                    "    ⚠  Folder contents changed after consolidation; "
                    "leaving the remaining folders untouched.",
                ))
                break
            try:
                empty = not os.listdir(directory.descriptor)
            except OSError:
                empty = False
            if not empty:
                continue
            if beets_integration._remove_exact_empty_merge_dir(
                    directory.parent_fd, directory.name, directory.descriptor):
                cleaned.add(directory.relative)
                log.info(fmt(
                    C.GREEN,
                    f"    ✓  Removed empty folder: {directory.path.name}.",
                ))
            else:
                log.info(fmt(
                    C.YELLOW,
                    f"    ⚠  Couldn't prove {directory.path.name} was still "
                    "the reviewed empty folder; left it in place.",
                ))
        return cleaned

    def close(self):
        if self._closed:
            return
        self._closed = True
        for item in self.files:
            item.close()

        def close_children(directory):
            for child in directory.children:
                close_children(child)
                try:
                    os.close(child.descriptor)
                except OSError:
                    pass

        close_children(self.root)
        for descriptor in reversed(self.album_chain):
            try:
                os.close(descriptor)
            except OSError:
                pass
        for descriptor in reversed(self.root_chain):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _open_held_file(parent_fd, name):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("safe consolidation file access is unavailable")
    descriptor = os.open(
        name,
        os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0),
        dir_fd=parent_fd,
    )
    try:
        if not _named_file_matches(parent_fd, name, descriptor):
            raise OSError("consolidation file changed while it was opened")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _populate_album_seal(seal):
    entry_count = 0
    audio_exts = set(config.AUDIO_EXTS)

    def walk(directory, depth):
        nonlocal entry_count
        if depth > _MAX_ALBUM_DEPTH:
            raise OSError("album directory nesting is too deep to consolidate safely")
        names = sorted(os.listdir(directory.descriptor))
        entry_count += len(names)
        if entry_count > _MAX_ALBUM_ENTRIES:
            raise OSError("album contains too many entries to consolidate safely")
        for name in names:
            value = os.stat(
                name, dir_fd=directory.descriptor, follow_symlinks=False)
            child_path = directory.path / name
            relative = directory.relative + (name,)
            if stat.S_ISDIR(value.st_mode):
                child_fd = beets_integration._open_exact_merge_dir(
                    directory.descriptor, name, value)
                child = _HeldDirectory(
                    child_path,
                    relative,
                    name,
                    child_fd,
                    directory.descriptor,
                )
                directory.children.append(child)
                walk(child, depth + 1)
                continue
            if not stat.S_ISREG(value.st_mode):
                raise OSError(
                    f"unsupported album entry prevents safe consolidation: {child_path}")
            descriptor = _open_held_file(directory.descriptor, name)
            lease = None
            try:
                metadata = None
                receipt = _file_receipt(descriptor)
                if child_path.suffix.lower() in audio_exts:
                    lease = acquire_inode_write_exclusion(descriptor)
                    if lease is None or not lease.intact():
                        raise OSError(
                            f"audio file has an active or uncertain writer: {child_path}")
                    metadata = _metadata_from_held_file(descriptor, child_path)
                    if (
                        not lease.intact()
                        or not _named_file_matches(
                            directory.descriptor, name, descriptor)
                        or not _same_held_file(descriptor, receipt)
                    ):
                        raise OSError(
                            f"audio file changed while metadata was read: {child_path}")
                held = _HeldFile(
                    child_path,
                    relative,
                    name,
                    directory.descriptor,
                    descriptor,
                    receipt,
                    metadata,
                    lease,
                )
                directory.files.append(held)
                seal.files.append(held)
                if metadata is not None:
                    seal.tracks.append(metadata)
                descriptor = None
                lease = None
            finally:
                if lease is not None:
                    lease.close()
                if descriptor is not None:
                    os.close(descriptor)
        if set(os.listdir(directory.descriptor)) != set(names):
            raise OSError("album contents changed while they were reviewed")

    walk(seal.root, 0)


def _seal_album(path, *, expected_identity=None):
    path = Path(os.path.abspath(os.fspath(path)))
    music_root = Path(os.path.abspath(os.fspath(config.MUSIC_ROOT)))
    try:
        relative = path.relative_to(music_root)
    except ValueError:
        raise OSError("album is outside the configured music library") from None
    if not relative.parts:
        raise OSError("the music library root is not an album")

    root_chain, root_names = beets_integration._open_absolute_merge_chain(
        music_root)
    album_chain = []
    seal = None
    try:
        album_chain, _created = beets_integration._open_merge_chain(
            root_chain[-1], tuple(relative.parts))
        if expected_identity is not None:
            current = os.fstat(album_chain[-1])
            if (
                not stat.S_ISDIR(current.st_mode)
                or (int(current.st_dev), int(current.st_ino)) != expected_identity
            ):
                raise OSError("sibling album changed before it was sealed")
        seal = _AlbumSeal(
            path,
            root_chain,
            root_names,
            album_chain,
            tuple(relative.parts),
        )
        _populate_album_seal(seal)
        if not seal.matches():
            raise OSError("album changed while it was sealed")
        return seal
    except BaseException:
        if seal is not None:
            seal.close()
        else:
            for descriptor in reversed(album_chain):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            for descriptor in reversed(root_chain):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        raise


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
    beets_integration._require_delete_journal_mode(connection)
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
            self, anchor, schema_receipt=None,
            item_rows=(), attribute_rows=(), album_rows=(),
            album_item_rows=(), album_attribute_rows=(),
            empty_album_ids=()):
        self.anchor = anchor
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
            or not beets_integration._beets_database_anchor_matches(self.anchor)
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
            or not beets_integration._beets_database_anchor_matches(self.anchor)
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
        descriptor = None if self.anchor is None else self.anchor["descriptor"]
        if descriptor is None:
            if (
                not beets_integration._beets_database_anchor_matches(self.anchor)
                or not before_matches()
            ):
                return False, None, "the reviewed files or database changed"
            result = mutate()
            if result is None or not result.complete:
                return False, result, "the protected backup was incomplete"
            if (
                not beets_integration._beets_database_anchor_matches(self.anchor)
                or not after_matches()
            ):
                return False, result, "the reviewed filesystem state changed"
            return True, result, None

        transaction = None
        result = None
        outcome = None
        caught = None
        try:
            timeout = getattr(config, "BEETS_TIMEOUT", 5)
            timeout = float(timeout) if timeout and timeout > 0 else 5.0
            transaction = AtomicSQLiteWrite(
                self.anchor,
                beets_integration._beets_database_anchor_matches,
                connect=sqlite3.connect,
                timeout=timeout,
            )
            connection = transaction.open()
            connection.execute("BEGIN IMMEDIATE")
            if not self._matches(connection, before_matches):
                raise OSError("the reviewed files or beets rows changed")
            # The recoverable copy must exist before database rows are
            # removed.
            result = mutate()
            if result is None or not result.complete:
                connection.rollback()
                outcome = (
                    False,
                    result,
                    "the protected backup was incomplete",
                )
            else:
                if not self._matches(connection, after_matches):
                    raise OSError("the reviewed state changed during backup")
                self._delete_rows(connection)
                if (
                    not beets_integration._beets_database_anchor_matches(
                        self.anchor)
                    or not after_matches()
                ):
                    raise OSError(
                        "the reviewed state changed before database commit")
                transaction.commit_and_publish(
                    after_matches,
                    lambda current: self._deletions_match(
                        current, after_matches),
                )
                outcome = True, result, None
        except BaseException as exc:
            caught = exc
        if transaction is not None:
            try:
                transaction.close()
            except BaseException as exc:
                if caught is None or (
                    isinstance(caught, Exception)
                    and not isinstance(exc, Exception)
                ):
                    caught = exc
                elif hasattr(caught, "add_note"):
                    caught.add_note(
                        "SQLite transaction cleanup also failed")
        if caught is not None:
            if not isinstance(caught, Exception):
                raise caught.with_traceback(caught.__traceback__)
            return False, result, str(caught)
        return outcome

    def close(self):
        if self._closed:
            return
        self._closed = True
        beets_integration._close_beets_database_anchor(self.anchor)


def _capture_beets_items(paths, anchors_match):
    anchor = beets_integration._open_beets_database_anchor()
    if anchor is None or anchor["descriptor"] is None:
        if not anchors_match():
            beets_integration._close_beets_database_anchor(anchor)
            raise OSError("reviewed album changed before database binding")
        return _BeetsItemSeal(anchor)

    try:
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
                anchor,
                schema_receipt,
                item_rows,
                attribute_rows,
                album_rows,
                album_item_rows,
                album_attribute_rows,
                tuple(empty_album_ids),
            )

        timeout = getattr(config, "BEETS_TIMEOUT", 5)
        timeout = float(timeout) if timeout and timeout > 0 else 5.0
        return inspect_sqlite_source(
            anchor,
            beets_integration._beets_database_anchor_matches,
            capture,
            connect=sqlite3.connect,
            timeout=timeout,
            anchors_match=anchors_match,
        )
    except BaseException:
        beets_integration._close_beets_database_anchor(anchor)
        raise


@dataclass
class _ConsolidationBinding:
    primary: _AlbumSeal
    sibling: _AlbumSeal
    files: tuple[_HeldFile, ...]
    database: _BeetsItemSeal
    used: bool = False

    @property
    def expected_receipts(self):
        return {
            "/".join(item.relative): item.receipt
            for item in self.files
        }

    def before_matches(self):
        return self.primary.matches() and self.sibling.matches()

    def after_matches(self):
        removed = {item.relative for item in self.files}
        return self.primary.matches() and self.sibling.matches(removed=removed)

    def close(self):
        self.database.close()


def _bind_summary(summary, primary, sibling):
    files = []
    seen = set()
    for track, _primary_track in summary["overlap"]:
        raw_path = track.get("path")
        held = sibling.held_file(raw_path) if raw_path else None
        if held is None or held.metadata is None or held.relative in seen:
            raise OSError("overlap no longer maps to one reviewed audio file")
        seen.add(held.relative)
        files.append(held)
    if not files:
        return summary
    anchors_match = lambda: primary.matches() and sibling.matches()
    database = _capture_beets_items(
        [item.path for item in files], anchors_match)
    summary["_binding"] = _ConsolidationBinding(
        primary, sibling, tuple(files), database)
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


def _sealed_sibling_albums(album, primary):
    primary_bare = strip_album_decorations(primary.path.name)
    primary_years = (
        _years_in(primary.path.name)
        or _years_in(album.get("title") or "")
    )
    api_bare = strip_album_decorations(album.get("title") or "")
    artist_fd = primary.album_chain[-2]
    try:
        names = sorted(os.listdir(artist_fd))
    except OSError as exc:
        raise OSError("couldn't enumerate the exact artist folder") from exc
    candidates = []
    primary_identity = (
        int(os.fstat(primary.root.descriptor).st_dev),
        int(os.fstat(primary.root.descriptor).st_ino),
    )
    for name in names:
        try:
            value = os.stat(name, dir_fd=artist_fd, follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISDIR(value.st_mode):
            continue
        identity = (int(value.st_dev), int(value.st_ino))
        if identity == primary_identity:
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
            sibling = _seal_album(
                primary.path.parent / name,
                expected_identity=identity,
            )
        except OSError as exc:
            log.info(fmt(
                C.YELLOW,
                f"  ⚠  Couldn't safely inspect {name} ({exc}); left it untouched.",
            ))
            continue
        if not primary.matches():
            sibling.close()
            for old, _score in candidates:
                old.close()
            raise OSError("primary album changed while siblings were inspected")
        candidates.append((sibling, score))
    return sorted(candidates, key=lambda item: -item[1])


def execute_consolidation(summary):
    """Move the overlapping sibling tracks to a recoverable backup instead of
    hard-deleting them, then return (removed_paths, n_failed) so the caller can
    drop exactly those files from the beets DB.

    Consolidation is the one destructive mode whose duplicate match can rest on a
    title+disc+(track)+duration-within-2s heuristic when neither side carries an
    ISRC/MBID, so a mistaken match would otherwise unlink a genuinely different
    recording with no way back. Routing removals through the gap-fill backup dir
    lets the retention sweep recover them, matching every other destructive
    mode's keep-a-backup stance."""
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
    paths = [item.path for item in binding.files]

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
        else:
            log.info(fmt(
                C.RED,
                "      ✗  consolidation stopped before an exact backup was "
                "committed; the reviewed files were left in place.",
            ))
        if reason:
            log.info(fmt(C.GRAY, f"         {reason}."))
        return [], attempted

    removed = [item.path for item in binding.files]
    if binding.database.tracked:
        log.info(fmt(C.GRAY, "    ⤷  Updated the beets library."))
    scanner.clear_scan_caches()
    binding.sibling.remove_empty_directories(binding.files)
    for path in removed:
        vlog(f"consolidation: moved {path} to protected backup")
    return removed, 0


def consolidate_albums(album, args):
    """Top-level consolidation flow. Always interactive - --yes does NOT silence it."""
    section("Consolidate similar album folders")
    # Consolidation deletes overlapping sibling tracks, so it must never run
    # under --dry-run - including on the "already complete" album path, which
    # reaches here before process_album's own dry-run stop.
    if getattr(args, "dry_run", False):
        log.info(fmt(C.GRAY,
            "  --dry-run: skipping consolidation (it would delete overlapping tracks)."))
        return 0

    primary_dir = catalog.find_album_dir_filesystem(album)
    if primary_dir is None:
        log.info(fmt(C.YELLOW, "  ⚠  Couldn't locate primary album folder after import."))
        log.info(fmt(C.GRAY,   "     Skipping consolidation."))
        return 0
    primary = None
    sibling_seals = []
    summaries = []
    try:
        try:
            primary = _seal_album(primary_dir)
        except OSError as exc:
            log.info(fmt(
                C.YELLOW,
                f"  ⚠  Couldn't safely inspect the primary album ({exc}).",
            ))
            log.info(fmt(C.GRAY, "     Skipping consolidation."))
            return 0
        log.info(fmt(C.GRAY, f"  Primary: {primary.path}"))
        if not primary.tracks:
            log.info(fmt(
                C.YELLOW,
                "  ⚠  No tracks read from primary folder. Skipping consolidation.",
            ))
            return 0

        try:
            candidates = _sealed_sibling_albums(album, primary)
        except OSError as exc:
            log.info(fmt(C.YELLOW, f"  ⚠  Consolidation stopped ({exc})."))
            return 0
        sibling_seals.extend(sibling for sibling, _score in candidates)
        if not candidates:
            log.info(fmt(
                C.GREEN,
                "  ✓  No sibling album folders found. Nothing to consolidate.",
            ))
            return 0

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
        n_actioned = 0

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
                f"    {overlap_count} track(s) overlap • "
                f"{len(summary['unique'])} bonus track(s) will remain",
            ))
            if warning:
                if quality["unknown"]:
                    log.info(fmt(
                        C.RED + C.BOLD,
                        f"    ⚠  {risky} track(s) here could lose quality "
                        f"({quality['losing_hires']} higher than primary, "
                        f"{quality['unknown']} uncertain or different layout)!",
                    ))
                else:
                    log.info(fmt(
                        C.RED + C.BOLD,
                        f"    ⚠  {quality['losing_hires']} track(s) here are "
                        "HIGHER quality than primary!",
                    ))
                print_per_track_consolidation(summary)

            log.info(fmt(C.WHITE, "    [d] delete overlapping tracks (default)"))
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
                    return n_actioned
                if choice == "d":
                    if warning:
                        log.info(fmt(
                            C.YELLOW + C.BOLD,
                            "    Type 'DELETE' to confirm a possible quality "
                            f"loss on {risky} track(s):",
                        ))
                        typed = ask("    > ", lower=False)
                        if typed != "DELETE":
                            log.info(fmt(
                                C.GRAY, "    Confirmation not given. Skipped."))
                            break
                    elif not confirm(
                        f"    Delete {overlap_count} track(s) from this sibling?",
                        default_yes=True,
                        auto_yes=False,
                    ):
                        log.info(fmt(C.GRAY, "    Skipped."))
                        break
                    deleted, failures = execute_consolidation(summary)
                    deleted_count = len(deleted)
                    if deleted_count:
                        log.info(fmt(
                            C.GREEN,
                            f"    ✓  Deleted {plural(deleted_count, 'track')}.",
                        ))
                    if failures:
                        log.info(fmt(
                            C.RED,
                            f"    ✗  Failed to delete {plural(failures, 'track')}.",
                        ))
                    n_actioned += deleted_count
                    break
                log.info(fmt(C.GRAY, "    Enter d / s / k / q."))

        return n_actioned
    finally:
        for summary in summaries:
            binding = summary.get("_binding")
            if isinstance(binding, _ConsolidationBinding):
                binding.close()
        for sibling in sibling_seals:
            sibling.close()
        if primary is not None:
            primary.close()
