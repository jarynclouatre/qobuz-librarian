"""Exact Beets row handling for crash-recoverable post-import relocation.

The caller owns the filesystem transaction and the bound database anchor.  This
module only captures the complete ``items`` and ``albums`` rows whose path field
will change, classifies those rows after a restart, and publishes an exact
compare-and-swap update through :class:`AtomicSQLiteWrite`.

Capture and classification accept an already reserved SQLite connection.  They
never open a configured path, so a missing Beets database cannot be created by
accident.
"""

from __future__ import annotations

import base64
import math
import os
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from enum import Enum
from typing import Any

from qobuz_librarian import config as cfg
from qobuz_librarian.library.scanner import is_library_system_folder
from qobuz_librarian.library.sqlite_atomic import AtomicSQLiteWrite

_CAPTURE_VERSION = 1
_TABLE_PATH_COLUMNS = (("items", "path"), ("albums", "artpath"))
_ROWID_ALIASES = frozenset({"rowid", "oid", "_rowid_"})
_SQLITE_INTEGER_MIN = -(2**63)
_SQLITE_INTEGER_MAX = 2**63 - 1


class DatabaseRowsState(str, Enum):
    """The state of every captured Beets row."""

    EMPTY = "empty"
    OLD = "old"
    NEW = "new"
    MIXED = "mixed"


def _sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sqlite_type(value: Any) -> str:
    if value is None:
        return "null"
    if type(value) is int:
        return "integer"
    if type(value) is float:
        return "real"
    if type(value) is str:
        return "text"
    if type(value) is bytes:
        return "blob"
    raise sqlite3.DataError("Beets row contains an unsupported SQLite value")


def encode_sqlite_value(value: Any) -> dict[str, Any]:
    """Encode one SQLite value without losing its storage class."""

    storage_class = _sqlite_type(value)
    kind = {"integer": "int", "real": "float"}.get(
        storage_class, storage_class
    )
    if kind == "null":
        encoded: Any = None
    elif kind == "int":
        if not _SQLITE_INTEGER_MIN <= value <= _SQLITE_INTEGER_MAX:
            raise sqlite3.DataError("SQLite integer is outside the signed 64-bit range")
        encoded = value
    elif kind == "float":
        if not math.isfinite(value):
            raise sqlite3.DataError("non-finite SQLite values cannot be compared exactly")
        encoded = value.hex()
    elif kind == "text":
        encoded = value
    else:
        encoded = base64.b64encode(value).decode("ascii")
    return {"type": kind, "value": encoded}


def decode_sqlite_value(encoded: object) -> Any:
    """Decode and strictly validate one value from a durable capture."""

    if not isinstance(encoded, dict) or set(encoded) != {"type", "value"}:
        raise ValueError("invalid encoded SQLite value")
    kind = encoded["type"]
    value = encoded["value"]
    if kind == "null":
        if value is not None:
            raise ValueError("invalid encoded SQLite null")
        return None
    if kind == "int":
        if (
            type(value) is not int
            or not _SQLITE_INTEGER_MIN <= value <= _SQLITE_INTEGER_MAX
        ):
            raise ValueError("invalid encoded SQLite integer")
        return value
    if kind == "float":
        if not isinstance(value, str):
            raise ValueError("invalid encoded SQLite real")
        try:
            decoded = float.fromhex(value)
        except ValueError as exc:
            raise ValueError("invalid encoded SQLite real") from exc
        if not math.isfinite(decoded) or decoded.hex() != value:
            raise ValueError("non-canonical encoded SQLite real")
        return decoded
    if kind == "text":
        if not isinstance(value, str):
            raise ValueError("invalid encoded SQLite text")
        return value
    if kind == "blob":
        if not isinstance(value, str):
            raise ValueError("invalid encoded SQLite blob")
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid encoded SQLite blob") from exc
        if base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError("non-canonical encoded SQLite blob")
        return decoded
    raise ValueError("unknown encoded SQLite storage class")


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = connection.execute(
        f"PRAGMA table_info({_sql_identifier(table)})"
    ).fetchall()
    columns = tuple(row[1] for row in rows)
    if (
        not columns
        or any(not isinstance(column, str) or not column for column in columns)
        or len(set(columns)) != len(columns)
        or _ROWID_ALIASES.intersection(column.casefold() for column in columns)
    ):
        raise sqlite3.DatabaseError(f"unrecognised Beets {table} table")
    return columns


def require_safe_relocation_database(connection: sqlite3.Connection) -> None:
    """Reject schemas or journal modes that make exact path updates unsafe."""

    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
    if journal_mode is None or str(journal_mode[0]).casefold() != "delete":
        raise sqlite3.DatabaseError(
            "safe Beets path updates require SQLite delete-journal mode"
        )
    trigger = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
        "AND lower(tbl_name) IN ('items', 'albums') LIMIT 1"
    ).fetchone()
    if trigger is not None:
        raise sqlite3.DatabaseError(
            "custom Beets path triggers make exact updates unverifiable"
        )
    for table, path_column in _TABLE_PATH_COLUMNS:
        columns = _columns(connection, table)
        if path_column not in columns:
            raise sqlite3.DatabaseError(
                f"Beets {table} table has no {path_column} column"
            )


def _absolute_path(value: os.PathLike[str] | str, *, label: str) -> str:
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise ValueError(f"{label} must be an absolute text path") from exc
    if not isinstance(raw, str) or not raw or "\x00" in raw or not os.path.isabs(raw):
        raise ValueError(f"{label} must be an absolute text path")
    return os.path.normpath(raw)


def _normalise_changes(
    path_changes: Mapping[os.PathLike[str] | str, os.PathLike[str] | str],
    music_root: os.PathLike[str] | str,
) -> tuple[dict[str, str], str]:
    root = _absolute_path(music_root, label="music root")
    changes: dict[str, str] = {}
    destinations: set[str] = set()
    for raw_source, raw_destination in path_changes.items():
        source = _absolute_path(raw_source, label="relocation source")
        destination = _absolute_path(raw_destination, label="relocation destination")
        try:
            if os.path.commonpath((root, source)) != root:
                raise ValueError("relocation source is outside the music library")
            if os.path.commonpath((root, destination)) != root:
                raise ValueError("relocation destination is outside the music library")
        except ValueError as exc:
            raise ValueError("relocation path is outside the music library") from exc
        if source == destination:
            raise ValueError("relocation source and destination are identical")
        if source in changes or destination in destinations:
            raise ValueError("relocation path mapping is ambiguous")
        changes[source] = destination
        destinations.add(destination)
    if set(changes).intersection(destinations):
        raise ValueError("relocation path mapping contains a chain or cycle")
    return changes, root


def _database_absolute_path(value: Any, music_root: str) -> str | None:
    if type(value) not in (str, bytes):
        return None
    raw = os.fsdecode(value) if type(value) is bytes else value
    if not raw or "\x00" in raw:
        return None
    if not os.path.isabs(raw):
        raw = os.path.join(music_root, raw)
    return os.path.normpath(os.path.abspath(raw))


def _replacement_database_path(old_value: str | bytes, destination: str, root: str):
    old_text = os.fsdecode(old_value) if type(old_value) is bytes else old_value
    replacement = destination if os.path.isabs(old_text) else os.path.relpath(destination, root)
    return os.fsencode(replacement) if type(old_value) is bytes else replacement


def _encoded_row(values: Iterable[Any]) -> list[dict[str, Any]]:
    return [encode_sqlite_value(value) for value in values]


def deleted_from_library(paths, music_root, *, besides=()) -> set:
    """The ``paths`` lstat finds missing from the music folder.

    None count while the music folder holds no artist folder other than
    ``besides``, the folders the current import made: an unmounted or
    emptied share reads that way. A path that can't be checked counts as
    present.
    """
    try:
        staging = os.stat(cfg.STAGING_DIR)
    except OSError:
        staging = None
    try:
        with os.scandir(music_root) as entries:
            mounted = any(
                entry.is_dir(follow_symlinks=False)
                and not is_library_system_folder(entry.name)
                and entry.name not in besides
                and not (staging is not None
                         and os.path.samestat(entry.stat(), staging))
                for entry in entries
            )
    except OSError:
        return set()
    if not mounted:
        return set()
    gone = set()
    for path in paths:
        try:
            os.lstat(path)
        except FileNotFoundError:
            gone.add(path)
        except OSError:
            pass
    return gone


def vanished_destination_rows(
    connection: sqlite3.Connection,
    path_changes: Mapping[os.PathLike[str] | str, os.PathLike[str] | str],
    *,
    music_root: os.PathLike[str] | str,
) -> list[tuple[str, int]]:
    """The rows already at a relocation destination whose file is gone.

    A folder deleted outside the app keeps its Beets rows, so an album filed
    there again meets rows for files that no longer exist.
    """
    changes, root = _normalise_changes(path_changes, music_root)
    destinations = set(changes.values())
    found = []
    for table, path_column in _TABLE_PATH_COLUMNS:
        for rowid, value in connection.execute(
            f"SELECT _rowid_, {_sql_identifier(path_column)} "
            f"FROM {_sql_identifier(table)}"
        ):
            absolute = _database_absolute_path(value, root)
            if absolute in destinations:
                found.append((table, rowid, absolute))
    besides = {
        os.path.relpath(path, root).split(os.sep)[0]
        for path in (*changes, *destinations)
    }
    gone = deleted_from_library(
        {path for _table, _rowid, path in found}, root, besides=besides
    )
    return [(table, rowid) for table, rowid, path in found if path in gone]


def forget_vanished_destination_rows(
    connection: sqlite3.Connection,
    path_changes: Mapping[os.PathLike[str] | str, os.PathLike[str] | str],
    *,
    music_root: os.PathLike[str] | str,
) -> int:
    """Delete the rows :func:`vanished_destination_rows` finds, in the
    caller's transaction. An album left with no tracks goes too, as `beet
    remove` would; one that keeps tracks only loses its cover path."""
    found = vanished_destination_rows(
        connection, path_changes, music_root=music_root
    )
    for table, rowid in found:
        if table == "albums":
            connection.execute(
                "UPDATE albums SET artpath = NULL WHERE _rowid_ = ?", (rowid,)
            )
            continue
        (album_id,) = connection.execute(
            "SELECT album_id FROM items WHERE _rowid_ = ?", (rowid,)
        ).fetchone()
        connection.execute(
            "DELETE FROM item_attributes WHERE entity_id = ?", (rowid,)
        )
        connection.execute("DELETE FROM items WHERE _rowid_ = ?", (rowid,))
        if album_id is not None and connection.execute(
            "SELECT 1 FROM items WHERE album_id = ? LIMIT 1", (album_id,)
        ).fetchone() is None:
            connection.execute(
                "DELETE FROM album_attributes WHERE entity_id = ?", (album_id,)
            )
            connection.execute("DELETE FROM albums WHERE id = ?", (album_id,))
    return len(found)


def capture_relocation_rows(
    connection: sqlite3.Connection,
    path_changes: Mapping[os.PathLike[str] | str, os.PathLike[str] | str],
    *,
    music_root: os.PathLike[str] | str,
    required_item_sources: Iterable[os.PathLike[str] | str] = (),
) -> dict[str, Any]:
    """Capture full rows affected by explicit absolute old-to-new paths.

    ``required_item_sources`` should contain every moved audio file.  Each must
    have exactly one matching ``items`` row; duplicate matches are refused for
    every mapped source, even when the caller did not mark it as audio.
    """

    changes, root = _normalise_changes(path_changes, music_root)
    required = {
        _absolute_path(path, label="required Beets item source")
        for path in required_item_sources
    }
    if not required.issubset(changes):
        raise ValueError("required Beets item source is absent from the path mapping")
    require_safe_relocation_database(connection)

    item_counts: Counter[str] = Counter()
    captured_tables = []
    for table, path_column in _TABLE_PATH_COLUMNS:
        columns = _columns(connection, table)
        path_index = columns.index(path_column)
        quoted = ", ".join(_sql_identifier(column) for column in columns)
        rows = []
        seen_rowids = set()
        for rowid, *values in connection.execute(
            f"SELECT _rowid_, {quoted} FROM {_sql_identifier(table)} "
            "ORDER BY _rowid_"
        ):
            if type(rowid) is not int or rowid in seen_rowids:
                raise sqlite3.DatabaseError(f"Beets {table} row identity is ambiguous")
            seen_rowids.add(rowid)
            values = tuple(values)
            absolute = _database_absolute_path(values[path_index], root)
            if absolute not in changes:
                continue
            old_value = values[path_index]
            if type(old_value) not in (str, bytes):
                raise sqlite3.DatabaseError(f"Beets {table} path has an invalid type")
            changed = list(values)
            changed[path_index] = _replacement_database_path(
                old_value, changes[absolute], root
            )
            rows.append(
                {
                    "rowid": encode_sqlite_value(rowid),
                    "old": _encoded_row(values),
                    "new": _encoded_row(changed),
                }
            )
            if table == "items":
                item_counts[absolute] += 1
        captured_tables.append(
            {
                "name": table,
                "columns": list(columns),
                "path_column": path_column,
                "rows": rows,
            }
        )

    duplicated = sorted(path for path, count in item_counts.items() if count > 1)
    missing_or_duplicated = sorted(
        path for path in required if item_counts[path] != 1
    )
    if duplicated or missing_or_duplicated:
        raise sqlite3.IntegrityError(
            "moved audio paths do not have one exact Beets item row"
        )
    capture = {
        "version": _CAPTURE_VERSION,
        "music_root": root,
        "tables": captured_tables,
    }
    if classify_relocation_rows(connection, capture) not in (
        DatabaseRowsState.EMPTY,
        DatabaseRowsState.OLD,
    ):
        raise sqlite3.IntegrityError(
            "a Beets row already occupies a relocation destination"
        )
    return capture


def _validated_capture(capture: object):
    if (
        not isinstance(capture, dict)
        or set(capture) != {"version", "music_root", "tables"}
        or capture.get("version") != _CAPTURE_VERSION
        or not isinstance(capture.get("tables"), list)
        or len(capture["tables"]) != len(_TABLE_PATH_COLUMNS)
    ):
        raise ValueError("invalid Beets relocation row capture")
    root = _absolute_path(capture.get("music_root"), label="captured music root")

    validated = []
    for raw_table, (expected_name, expected_path_column) in zip(
        capture["tables"], _TABLE_PATH_COLUMNS, strict=True
    ):
        if (
            not isinstance(raw_table, dict)
            or set(raw_table) != {"name", "columns", "path_column", "rows"}
            or raw_table.get("name") != expected_name
            or raw_table.get("path_column") != expected_path_column
            or not isinstance(raw_table.get("columns"), list)
            or not isinstance(raw_table.get("rows"), list)
        ):
            raise ValueError("invalid Beets relocation table capture")
        columns = tuple(raw_table["columns"])
        if (
            not columns
            or any(not isinstance(column, str) or not column for column in columns)
            or len(set(columns)) != len(columns)
            or expected_path_column not in columns
        ):
            raise ValueError("invalid Beets relocation columns")
        path_index = columns.index(expected_path_column)
        rows = []
        seen_rowids = set()
        for raw_row in raw_table["rows"]:
            if (
                not isinstance(raw_row, dict)
                or set(raw_row) != {"rowid", "old", "new"}
                or not isinstance(raw_row.get("old"), list)
                or not isinstance(raw_row.get("new"), list)
                or len(raw_row["old"]) != len(columns)
                or len(raw_row["new"]) != len(columns)
            ):
                raise ValueError("invalid Beets relocation row capture")
            rowid = decode_sqlite_value(raw_row["rowid"])
            old = tuple(decode_sqlite_value(value) for value in raw_row["old"])
            new = tuple(decode_sqlite_value(value) for value in raw_row["new"])
            if type(rowid) is not int or rowid in seen_rowids:
                raise ValueError("invalid Beets relocation row identity")
            seen_rowids.add(rowid)
            if any(
                raw_row["old"][index] != raw_row["new"][index]
                for index in range(len(columns))
                if index != path_index
            ) or raw_row["old"][path_index] == raw_row["new"][path_index]:
                raise ValueError("Beets relocation row changes more than its path")
            old_path = _database_absolute_path(old[path_index], root)
            new_path = _database_absolute_path(new[path_index], root)
            if old_path is None or new_path is None or old_path == new_path:
                raise ValueError("invalid Beets relocation path change")
            if (
                os.path.commonpath((root, old_path)) != root
                or os.path.commonpath((root, new_path)) != root
            ):
                raise ValueError("captured Beets relocation leaves the music library")
            rows.append((rowid, old, new))
        validated.append((expected_name, columns, path_index, tuple(rows)))
    return root, tuple(validated)


def _classify_validated(
    connection: sqlite3.Connection, root: str, validated
) -> DatabaseRowsState:
    saw_row = False
    saw_old = False
    saw_new = False
    for table, columns, path_index, rows in validated:
        if _columns(connection, table) != columns:
            raise sqlite3.DatabaseError(f"Beets {table} schema changed")
        quoted = ", ".join(_sql_identifier(column) for column in columns)
        select_one = (
            f"SELECT {quoted} FROM {_sql_identifier(table)} WHERE _rowid_ = ?"
        )
        for rowid, old, new in rows:
            saw_row = True
            current = connection.execute(select_one, (rowid,)).fetchone()
            if current is None:
                return DatabaseRowsState.MIXED
            encoded = tuple(encode_sqlite_value(value) for value in current)
            if encoded == tuple(map(encode_sqlite_value, old)):
                saw_old = True
            elif encoded == tuple(map(encode_sqlite_value, new)):
                saw_new = True
            else:
                return DatabaseRowsState.MIXED
            if saw_old and saw_new:
                return DatabaseRowsState.MIXED
        expected_destination_owners: dict[str, set[int]] = {}
        for rowid, _old, new in rows:
            destination = _database_absolute_path(new[path_index], root)
            if destination is None:
                raise ValueError("invalid captured Beets destination")
            expected_destination_owners.setdefault(destination, set()).add(rowid)
        current_destination_owners = {
            destination: set() for destination in expected_destination_owners
        }
        if current_destination_owners:
            path_column = _sql_identifier(columns[path_index])
            for rowid, value in connection.execute(
                f"SELECT _rowid_, {path_column} FROM {_sql_identifier(table)}"
            ):
                absolute = _database_absolute_path(value, root)
                if absolute in current_destination_owners:
                    current_destination_owners[absolute].add(rowid)
            expected = expected_destination_owners if saw_new else {
                destination: set() for destination in expected_destination_owners
            }
            if current_destination_owners != expected:
                return DatabaseRowsState.MIXED
    if not saw_row:
        return DatabaseRowsState.EMPTY
    return DatabaseRowsState.NEW if saw_new else DatabaseRowsState.OLD


def classify_relocation_rows(
    connection: sqlite3.Connection, capture: object
) -> DatabaseRowsState:
    """Classify a durable capture as wholly old, wholly new, mixed, or empty."""

    root, validated = _validated_capture(capture)
    require_safe_relocation_database(connection)
    return _classify_validated(connection, root, validated)


def _exact_update(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    path_index: int,
    rowid: int,
    old: tuple[Any, ...],
    new: tuple[Any, ...],
) -> None:
    where = ["_rowid_ = ?", "typeof(_rowid_) = 'integer'"]
    parameters: list[Any] = [rowid]
    for column, value in zip(columns, old, strict=True):
        quoted = _sql_identifier(column)
        where.extend((f"{quoted} IS ?", f"typeof({quoted}) = ?"))
        parameters.extend((value, _sqlite_type(value)))
    path_column = _sql_identifier(columns[path_index])
    updated = connection.execute(
        f"UPDATE {_sql_identifier(table)} SET {path_column} = ? WHERE "
        + " AND ".join(where),
        [new[path_index], *parameters],
    )
    if updated.rowcount != 1:
        raise sqlite3.IntegrityError("Beets row changed during exact path update")


def publish_relocation_rows(
    anchor: dict[str, Any],
    anchor_matches: Callable[[dict[str, Any]], bool],
    authority_matches: Callable[[], bool],
    capture: object,
    *,
    connect: Callable[..., sqlite3.Connection] = sqlite3.connect,
    timeout: float = 5.0,
) -> DatabaseRowsState:
    """Publish captured old rows as new through an atomic database replacement.

    ``authority_matches`` is supplied by the filesystem transaction and must
    prove its source, destination, journal, and operation authority each time it
    is called.  A wholly-new capture is an idempotent success; mixed rows are
    always refused.
    """

    root, validated = _validated_capture(capture)
    if not callable(authority_matches) or not authority_matches():
        raise OSError("relocation authority changed before the Beets update")
    if anchor is None or anchor.get("descriptor") is None:
        raise OSError("an existing bound Beets database is required")

    transaction = AtomicSQLiteWrite(
        anchor,
        anchor_matches,
        connect=connect,
        timeout=timeout,
    )
    try:
        connection = transaction.open()
        connection.execute("BEGIN IMMEDIATE")
        require_safe_relocation_database(connection)
        if not authority_matches():
            raise OSError("relocation authority changed during the Beets update")
        state = _classify_validated(connection, root, validated)
        if state in (DatabaseRowsState.EMPTY, DatabaseRowsState.NEW):
            connection.rollback()
            if not authority_matches():
                raise OSError("relocation authority changed during the Beets update")
            return state
        if state is not DatabaseRowsState.OLD:
            raise sqlite3.IntegrityError(
                "captured Beets rows are neither wholly old nor wholly new"
            )
        for table, columns, path_index, rows in validated:
            for rowid, old, new in rows:
                _exact_update(
                    connection, table, columns, path_index, rowid, old, new
                )
        if (
            _classify_validated(connection, root, validated)
            is not DatabaseRowsState.NEW
        ):
            raise sqlite3.IntegrityError("Beets path updates were not exact")
        if not authority_matches():
            raise OSError("relocation authority changed during the Beets update")
        transaction.commit_and_publish(
            authority_matches,
            lambda current: (
                _classify_validated(current, root, validated)
                is DatabaseRowsState.NEW
            ),
        )
        if not authority_matches():
            raise OSError("relocation authority changed after the Beets update")
        return DatabaseRowsState.NEW
    finally:
        transaction.close()


def rollback_relocation_rows(
    anchor: dict[str, Any],
    anchor_matches: Callable[[dict[str, Any]], bool],
    authority_matches: Callable[[], bool],
    capture: object,
    *,
    connect: Callable[..., sqlite3.Connection] = sqlite3.connect,
    timeout: float = 5.0,
) -> DatabaseRowsState:
    """Restore captured new rows to their exact old paths atomically.

    This is the inverse of :func:`publish_relocation_rows`.  It exists for a
    committed relocation whose external ownership hand-off was interrupted:
    recovery can put Beets back on the still-intact source before removing the
    additive public copies.  Mixed or otherwise changed rows are never guessed
    at.
    """

    root, validated = _validated_capture(capture)
    if not callable(authority_matches) or not authority_matches():
        raise OSError("relocation authority changed before the Beets rollback")
    if anchor is None or anchor.get("descriptor") is None:
        raise OSError("an existing bound Beets database is required")

    transaction = AtomicSQLiteWrite(
        anchor,
        anchor_matches,
        connect=connect,
        timeout=timeout,
    )
    try:
        connection = transaction.open()
        connection.execute("BEGIN IMMEDIATE")
        require_safe_relocation_database(connection)
        if not authority_matches():
            raise OSError("relocation authority changed during the Beets rollback")
        state = _classify_validated(connection, root, validated)
        if state in (DatabaseRowsState.EMPTY, DatabaseRowsState.OLD):
            connection.rollback()
            if not authority_matches():
                raise OSError(
                    "relocation authority changed during the Beets rollback"
                )
            return state
        if state is not DatabaseRowsState.NEW:
            raise sqlite3.IntegrityError(
                "captured Beets rows are neither wholly new nor wholly old"
            )
        for table, columns, path_index, rows in validated:
            for rowid, old, new in rows:
                _exact_update(
                    connection, table, columns, path_index, rowid, new, old
                )
        if (
            _classify_validated(connection, root, validated)
            is not DatabaseRowsState.OLD
        ):
            raise sqlite3.IntegrityError("Beets path rollback was not exact")
        if not authority_matches():
            raise OSError("relocation authority changed during the Beets rollback")
        transaction.commit_and_publish(
            authority_matches,
            lambda current: (
                _classify_validated(current, root, validated)
                is DatabaseRowsState.OLD
            ),
        )
        if not authority_matches():
            raise OSError("relocation authority changed after the Beets rollback")
        return DatabaseRowsState.OLD
    finally:
        transaction.close()


__all__ = [
    "DatabaseRowsState",
    "capture_relocation_rows",
    "classify_relocation_rows",
    "decode_sqlite_value",
    "deleted_from_library",
    "encode_sqlite_value",
    "forget_vanished_destination_rows",
    "publish_relocation_rows",
    "rollback_relocation_rows",
    "require_safe_relocation_database",
    "vanished_destination_rows",
]
