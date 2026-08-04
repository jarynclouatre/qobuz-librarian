"""One-run Beets hook for exact single-track Undo ownership evidence."""

from __future__ import annotations

import json
import os
import secrets
import stat
import sys
import threading

from beets import plugins
from beets.plugins import BeetsPlugin

from qobuz_librarian.integrations.beets import (
    _MANAGED_OWNERSHIP_MODE,
    _MANAGED_SNAPSHOT_VERSION,
    _OWNERSHIP_MARKER,
    _OWNERSHIP_MAX_SOURCE_ROOTS,
    _managed_payload_hash,
    _merge_absolute_parts,
    _merge_chain_is_named,
    _open_absolute_merge_chain,
    _open_ownership_dir,
    _ownership_identity,
    _ownership_marker_token,
    _ownership_relative_parts,
    _reclaim_ownership_directories,
    _remove_exact_ownership_marker,
    _rename_ownership_noreplace,
    _staged_identity,
    _valid_managed_snapshot,
    _write_managed_snapshot,
)
from qobuz_librarian.recovery import decode_recovery_json

_MANIFEST_FD_ENV = "QOBUZ_LIBRARIAN_OWNERSHIP_FD"
_NONCE_ENV = "QOBUZ_LIBRARIAN_OWNERSHIP_NONCE"
_SOURCE_ROOTS_ENV = "QOBUZ_LIBRARIAN_OWNERSHIP_SOURCE_ROOTS"
_MODE_ENV = "QOBUZ_LIBRARIAN_OWNERSHIP_MODE"
_MAX_MANIFEST_BYTES = 1024 * 1024
_COPY_COMPARE_CHUNK = 1024 * 1024


def _valid_nonce(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _art_guard_module():
    module = sys.modules.get("beetsplug.qobuz_art_guard")
    if module is not None:
        return module
    try:
        from qobuz_librarian.integrations.beets_plugins import (
            qobuz_art_guard,
        )
    except ImportError:
        return None
    return qobuz_art_guard


def _suppress_fetchart_candidate(task):
    for plugin in plugins.find_plugins():
        candidates = getattr(plugin, "art_candidates", None)
        if (
            getattr(plugin, "name", None) == "fetchart"
            and isinstance(candidates, dict)
        ):
            candidates.pop(task, None)


def _positive_item_number(item, field):
    try:
        value = getattr(item, field)
    except (AttributeError, TypeError, ValueError):
        try:
            value = item.get(field)
        except (AttributeError, TypeError, ValueError):
            return None
    if type(value) is int:
        number = value
    elif isinstance(value, str) and value.isdecimal():
        number = int(value)
    else:
        return None
    return number if number > 0 else None


def _album_scope_parts(item, destination_parts):
    """Derive the album directory from Beets' exact item destination."""
    if not isinstance(destination_parts, (list, tuple)):
        return None
    parents = tuple(destination_parts[:-1])
    if not parents:
        return None
    disc_total = _positive_item_number(item, "disctotal")
    disc = _positive_item_number(item, "disc")
    if disc_total is not None and disc_total > 1:
        if disc is None or disc > disc_total:
            return None
        try:
            leaf = os.fsdecode(parents[-1]).casefold()
        except (TypeError, UnicodeError, ValueError):
            return None
        # Beets types $disc as PaddedInt(2), so the bundled template files this
        # disc as "Disc 01" — comparing against an unpadded "disc 1" matched
        # nothing and bound the scope to the disc folder on every stock
        # multi-disc import. Still strip only THIS item's disc.
        for prefix in ("disc", "cd"):
            if not leaf.startswith(prefix):
                continue
            number = leaf[len(prefix):].strip()
            if (
                number.isascii()
                and number.isdigit()
                and int(number) == disc
            ):
                parents = parents[:-1]
            break
    return parents or None


class QobuzOwnershipPlugin(BeetsPlugin):
    """Record only a destination proven to contain the staged source bytes."""

    def __init__(self):
        super().__init__()
        self._lock = threading.RLock()
        self._fd = self._manifest_fd()
        self._nonce = os.environ.get(_NONCE_ENV, "")
        self._source_roots = self._read_source_roots()
        mode = os.environ.get(_MODE_ENV, "")
        self._mode_valid = mode in ("", _MANAGED_OWNERSHIP_MODE)
        self._managed = mode == _MANAGED_OWNERSHIP_MODE
        self._managed_intent = self._read_managed_intent()
        self._managed_sources = {
            os.path.abspath(os.fsencode(item["path"])): item
            for item in (self._managed_intent or ())
        }
        self._managed_owner = None
        self._managed_root = None
        self._managed_root_identity = None
        self._managed_generation = 0
        self._managed_previous_hash = None
        if self._managed_intent:
            first = self._managed_intent[0]["_snapshot"]
            self._managed_owner = first["owner"]
            self._managed_root = os.path.abspath(os.fsencode(first["root"]))
            self._managed_root_identity = first["root_identity"]
            self._managed_previous_hash = first["hash"]
            self._managed_generation = first["generation"]
            self._managed_intent = tuple(
                {key: value for key, value in item.items() if key != "_snapshot"}
                for item in self._managed_intent
            )
            self._managed_sources = {
                os.path.abspath(os.fsencode(item["path"])): item
                for item in self._managed_intent
            }
        self._enabled = False
        self._managed_armed = False
        self._managed_begin_registered = False
        self._preserve_created = False
        self._root = None
        self._root_fd = None
        self._root_chain = ()
        self._root_names = ()
        self._root_device_inode = None
        self._created = {}
        self._source_item = None
        self._source_items = []
        managed_ready = (
            self._mode_valid
            and (not self._managed or bool(self._managed_intent))
        )
        ready = (
            self._fd is not None
            and _valid_nonce(self._nonce)
            and self._source_roots
            and managed_ready
        )
        if self._managed:
            self.register_listener("import_begin", self._begin)
            self._managed_begin_registered = True
        if ready:
            self.import_stages = [self._prepare_destinations]
            if not self._managed:
                self.register_listener("import_begin", self._begin)
            self.register_listener("before_item_moved", self._before_item_moved)
            self.register_listener("item_moved", self._item_moved)
            self.register_listener("item_copied", self._item_copied)
            self.register_listener("cli_exit", self._seal)

    def _managed_intent_is_current(self):
        if not self._managed or not self._managed_intent:
            return False
        fresh = self._read_managed_intent()
        if not fresh:
            return False
        launch = fresh[0]["_snapshot"]
        intent = tuple(
            {key: value for key, value in item.items() if key != "_snapshot"}
            for item in fresh
        )
        return (
            all(item.get("_snapshot") == launch for item in fresh)
            and intent == self._managed_intent
            and launch["owner"] == self._managed_owner
            and os.path.abspath(os.fsencode(launch["root"]))
            == self._managed_root
            and launch["root_identity"] == self._managed_root_identity
            and launch["generation"] == self._managed_generation == 1
            and launch["hash"] == self._managed_previous_hash
        )

    def arm_managed_import(self, nonce):
        """Arm the one import-begin gate after same-process plugin loading."""
        with self._lock:
            ready = (
                self._managed
                and self._mode_valid
                and not self._managed_armed
                and not self._enabled
                and nonce == self._nonce
                and _valid_nonce(nonce)
                and self._fd is not None
                and bool(self._source_roots)
                and self._managed_begin_registered
                and self.import_stages == [self._prepare_destinations]
                and list(plugins.find_plugins())[-1:] == [self]
                and self._managed_intent_is_current()
            )
            if ready:
                self._managed_armed = True
            return ready is True

    @staticmethod
    def _manifest_fd():
        try:
            value = int(os.environ.get(_MANIFEST_FD_ENV, ""))
        except (TypeError, ValueError):
            return None
        return value if value >= 3 else None

    @staticmethod
    def _read_source_roots():
        try:
            values = json.loads(os.environ.get(_SOURCE_ROOTS_ENV, ""))
        except (TypeError, ValueError):
            return ()
        if (
            not isinstance(values, list)
            or not 0 < len(values) <= _OWNERSHIP_MAX_SOURCE_ROOTS
        ):
            return ()
        roots = []
        for value in values:
            if not isinstance(value, str) or not value or "\x00" in value:
                return ()
            root = os.path.abspath(os.fsencode(value))
            if root == os.path.dirname(root):
                return ()
            if any(
                root == existing
                or root.startswith(existing + os.fsencode(os.sep))
                or existing.startswith(root + os.fsencode(os.sep))
                for existing in roots
            ):
                return ()
            roots.append(root)
        return tuple(roots)

    def _read_managed_intent(self):
        if not getattr(self, "_managed", False) or self._fd is None:
            return ()
        try:
            manifest_stat = os.fstat(self._fd)
            if (
                not stat.S_ISREG(manifest_stat.st_mode)
                or manifest_stat.st_uid != os.geteuid()
                or stat.S_IMODE(manifest_stat.st_mode) != 0o600
                or not 0 < manifest_stat.st_size <= _MAX_MANIFEST_BYTES
            ):
                return ()
            raw = os.pread(self._fd, manifest_stat.st_size + 1, 0)
            if len(raw) != manifest_stat.st_size or not raw.endswith(b"\n"):
                return ()
            lines = raw.splitlines()
            if len(lines) != 2:
                return ()
            origin, launch = (
                decode_recovery_json(line, max_bytes=_MAX_MANIFEST_BYTES)
                for line in lines
            )
            expected_launch = {
                **origin,
                "generation": 1,
                "previous_hash": origin.get("hash"),
            }
            expected_launch["hash"] = _managed_payload_hash(expected_launch)
            if (
                not _valid_managed_snapshot(origin)
                or not _valid_managed_snapshot(launch)
                or origin["version"] != _MANAGED_SNAPSHOT_VERSION
                or origin["generation"] != 0
                or origin["previous_hash"] is not None
                or origin["hash"] != _managed_payload_hash(origin)
                or origin["nonce"] != self._nonce
                or origin["sealed"] is not False
                or origin["mappings"]
                or origin["cleanup_directories"]
                or launch != expected_launch
            ):
                return ()
            return tuple({**item, "_snapshot": launch}
                         for item in launch["intent"])
        except (OSError, TypeError, ValueError):
            return ()

    def _source_item_path(self, item):
        try:
            path = os.path.abspath(os.fsencode(item.path))
            for root in self._source_roots:
                if path != root and os.path.commonpath((root, path)) == root:
                    if (
                        getattr(self, "_managed", False)
                        and path not in self._managed_sources
                    ):
                        return None, None
                    return root, path
        except (AttributeError, OSError, TypeError, ValueError):
            pass
        return None, None

    def _begin(self, session):
        with self._lock:
            root_chain = ()
            try:
                if getattr(self, "_managed", False):
                    if not self._managed_armed:
                        raise OSError("managed import was not armed")
                    self._managed_armed = False
                    if not self._managed_intent_is_current():
                        raise OSError("managed import intent changed")
                manifest_stat = os.fstat(self._fd)
                if (
                    not stat.S_ISREG(manifest_stat.st_mode)
                    or manifest_stat.st_uid != os.geteuid()
                    or manifest_stat.st_mode & 0o077
                    or list(plugins.find_plugins())[-1] is not self
                ):
                    if getattr(self, "_managed", False):
                        raise OSError("managed ownership gate changed")
                    return
                root = os.path.abspath(os.fsencode(session.lib.directory))
                if root == os.path.dirname(root):
                    if getattr(self, "_managed", False):
                        raise OSError("managed library root is unsafe")
                    return
                if (
                    getattr(self, "_managed", False)
                    and root != self._managed_root
                ):
                    raise OSError("managed library root changed")
                root_chain, root_names = _open_absolute_merge_chain(root)
                root_fd = root_chain[-1]
                root_stat = os.fstat(root_fd)
                if getattr(self, "_managed", False) and (
                    int(root_stat.st_dev)
                    != self._managed_root_identity["device"]
                    or int(root_stat.st_ino)
                    != self._managed_root_identity["inode"]
                ):
                    raise OSError("managed library root identity changed")
                self._root = root
                self._root_fd = root_fd
                self._root_chain = root_chain
                self._root_names = root_names
                root_chain = ()
                self._root_device_inode = (
                    int(root_stat.st_dev),
                    int(root_stat.st_ino),
                )
                if not self._root_chain_is_named_locked():
                    raise OSError("library root changed before import")
                self._enabled = True
                self._write_locked(sealed=False, items=[])
            except BaseException as exc:
                self._close_locked()
                if getattr(self, "_managed", False) or not isinstance(
                    exc, Exception
                ):
                    raise
            finally:
                for descriptor in reversed(root_chain):
                    try:
                        os.close(descriptor)
                    except BaseException:
                        pass

    def _prepare_destinations(self, session, task):
        del session
        with self._lock:
            if not self._enabled:
                if getattr(self, "_managed", False):
                    raise OSError("managed ownership gate is not active")
                return
            try:
                for item in task.imported_items():
                    root, source_path = self._source_item_path(item)
                    if source_path is None:
                        if getattr(self, "_managed", False):
                            raise ValueError(
                                "managed import contains an undeclared source")
                        continue
                    if any(
                        selected["source_path"] == source_path
                        for selected in self._source_items
                    ):
                        raise ValueError("ownership import contains a duplicate item")
                    source_parent_fd = None
                    source_fd = None
                    selected = None
                    try:
                        source_parent_fd, source_name, source_fd = (
                            self._hold_source_leaf_locked(root, source_path)
                        )
                        declaration = getattr(
                            self, "_managed_sources", {}).get(source_path)
                        if getattr(self, "_managed", False) and (
                            declaration is None
                            or _staged_identity(os.fstat(source_fd))
                            != tuple(declaration["identity"])
                        ):
                            raise ValueError("managed import source changed")
                        destination = item.destination(
                            relative_to_libdir=True)
                        parts = _ownership_relative_parts(
                            os.fsdecode(destination))
                        if not parts:
                            raise ValueError("unsafe ownership destination")
                        album_scope = _album_scope_parts(item, parts)
                        if album_scope is None:
                            raise ValueError(
                                "ownership album scope is unavailable")
                        selected = {
                            "task": task,
                            "item": item,
                            "source_path": source_path,
                            "source_parent_fd": source_parent_fd,
                            "source_name": source_name,
                            "source_fd": source_fd,
                            "destination_parent": parts[:-1],
                            "album_scope_parts": album_scope,
                            "pending_move": None,
                            "destination": None,
                            "destination_identity": None,
                            "move_proven": False,
                            "managed_declaration": declaration,
                        }
                        self._source_items.append(selected)
                    finally:
                        transferred = (
                            selected is not None
                            and any(
                                record is selected
                                for record in self._source_items
                            )
                        )
                        if not transferred:
                            for descriptor in (source_fd, source_parent_fd):
                                if descriptor is not None:
                                    try:
                                        os.close(descriptor)
                                    except OSError:
                                        pass
                    if self._source_item is None:
                        self._source_item = selected
                    self._ensure_parent_locked(parts[:-1])
                    if not any(
                        existing["task"] is task
                        for existing in self._source_items[:-1]
                    ):
                        self._authorise_art_locked(
                            task, parts[:-1], selected)
                if self._source_item is None:
                    raise ValueError("ownership source was not found")
                self._write_locked(sealed=False, items=[])
            except BaseException as exc:
                cleanup_error = None
                try:
                    self._close_locked()
                except BaseException as cleanup_exc:
                    cleanup_error = cleanup_exc
                if cleanup_error is not None and not isinstance(
                        cleanup_error, Exception):
                    if hasattr(cleanup_error, "add_note"):
                        cleanup_error.add_note(
                            f"ownership destination preparation also "
                            f"failed: {exc}")
                    raise cleanup_error.with_traceback(
                        cleanup_error.__traceback__)
                if not isinstance(exc, Exception):
                    if cleanup_error is not None and hasattr(exc, "add_note"):
                        exc.add_note(
                            f"ownership cleanup also failed: {cleanup_error}")
                    raise
                if getattr(self, "_managed", False):
                    if cleanup_error is not None and hasattr(exc, "add_note"):
                        exc.add_note(
                            f"ownership cleanup also failed: {cleanup_error}")
                    raise exc.with_traceback(exc.__traceback__)
                if cleanup_error is not None:
                    raise cleanup_error.with_traceback(
                        cleanup_error.__traceback__)

    def _authorise_art_locked(self, task, parent_parts, selected):
        module = _art_guard_module()
        relative = os.fsencode(os.sep).join(parent_parts)
        record = self._created.get(relative)
        authorised = False
        if (
            module is not None
            and self._root_fd is not None
            and record is not None
            and record.get("published") is True
        ):
            try:
                current = self._open_created_locked(parent_parts)
                held = os.fstat(record["fd"])
                if _ownership_identity(current) == _ownership_identity(held):
                    authorised = module.authorise_ownership_art(
                        task,
                        self._root_fd,
                        record,
                        selected["item"],
                    )
            except (OSError, TypeError, ValueError):
                authorised = False
        if not authorised:
            _suppress_fetchart_candidate(task)

    def _hold_source_leaf_locked(self, root, source_path):
        relative = os.path.relpath(source_path, root)
        parts = _ownership_relative_parts(os.fsdecode(relative))
        if not parts:
            raise OSError("unsafe ownership source")
        current = None
        source_fd = None
        transferred = False
        try:
            current = _open_ownership_dir(root)
            for part in parts[:-1]:
                child = _open_ownership_dir(part, dir_fd=current)
                try:
                    os.close(current)
                except BaseException:
                    try:
                        os.close(child)
                    except BaseException:
                        pass
                    raise
                current = child
            nofollow = getattr(os, "O_NOFOLLOW", None)
            if nofollow is None:
                raise OSError("safe source access is unavailable")
            flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
            source_fd = os.open(parts[-1], flags, dir_fd=current)
            if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                raise OSError("ownership source is not a regular file")
            transferred = True
            return current, parts[-1], source_fd
        finally:
            if not transferred:
                for descriptor in (source_fd, current):
                    if descriptor is not None:
                        try:
                            os.close(descriptor)
                        except BaseException:
                            pass

    def _ensure_parent_locked(self, parts):
        if self._root_fd is None:
            raise OSError("library root is unavailable")
        chain = [os.dup(self._root_fd)]
        names = []
        try:
            relative = []
            for part in parts:
                if not self._parent_chain_is_named_locked(chain, names):
                    raise OSError("ownership parent chain changed")
                relative.append(part)
                try:
                    child = _open_ownership_dir(part, dir_fd=chain[-1])
                except FileNotFoundError:
                    child = self._create_parent_locked(
                        chain, names, relative, part
                    )
                chain.append(child)
                names.append(part)
                if not self._parent_chain_is_named_locked(chain, names):
                    raise OSError("ownership parent chain changed")
        finally:
            for descriptor in reversed(chain):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _parent_chain_is_named_locked(self, chain, names):
        if (
            not self._root_chain_is_named_locked()
            or len(chain) != len(names) + 1
        ):
            return False
        try:
            held_root = os.fstat(self._root_fd)
            chain_root = os.fstat(chain[0])
            if (
                _ownership_identity(chain_root)
                != _ownership_identity(held_root)
            ):
                return False
            for index, name in enumerate(names):
                named = os.stat(
                    name, dir_fd=chain[index], follow_symlinks=False
                )
                held = os.fstat(chain[index + 1])
                if (
                    not stat.S_ISDIR(named.st_mode)
                    or not stat.S_ISDIR(held.st_mode)
                    or _ownership_identity(named)
                    != _ownership_identity(held)
                ):
                    return False
        except (OSError, TypeError, ValueError):
            return False
        return True

    def _root_chain_is_named_locked(self):
        """Prove every public component still names the held library root."""
        if self._root is None or self._root_fd is None:
            return False
        chain = getattr(self, "_root_chain", None)
        names = getattr(self, "_root_names", None)
        if chain is None and names is None:
            # Compatibility for older, synthetic plugin instances used by
            # the version-one evidence tests. Runtime instances always have
            # the complete chain fields initialised by __init__.
            try:
                named = os.stat(self._root, follow_symlinks=False)
                held = os.fstat(self._root_fd)
                return (
                    stat.S_ISDIR(named.st_mode)
                    and _ownership_identity(named)
                    == _ownership_identity(held)
                )
            except (OSError, TypeError, ValueError):
                return False
        if (
            not isinstance(chain, (list, tuple))
            or not chain
            or not isinstance(names, tuple)
            or len(chain) != len(names) + 1
            or chain[-1] != self._root_fd
        ):
            return False
        try:
            root, expected_names = _merge_absolute_parts(self._root)
            held = os.fstat(self._root_fd)
            return (
                os.fspath(root) == os.path.abspath(os.fsdecode(self._root))
                and names == expected_names
                and stat.S_ISDIR(held.st_mode)
                and (
                    int(held.st_dev),
                    int(held.st_ino),
                ) == self._root_device_inode
                and _merge_chain_is_named(chain, names)
            )
        except (OSError, TypeError, ValueError):
            return False

    def _discard_created_at_parent_locked(self, parent_fd, name, record):
        try:
            if not self._remove_record_marker_locked(record):
                return False
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            held = os.fstat(record["fd"])
            if (
                not stat.S_ISDIR(named.st_mode)
                or _ownership_identity(named) != _ownership_identity(held)
            ):
                return False
            os.rmdir(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except (OSError, TypeError, ValueError):
            return False
        key = os.fsencode(record["relative"])
        self._discard_created_locked(key, record)
        self._write_locked(sealed=False, items=[])
        return True

    def _create_parent_locked(
        self, parent_chain, parent_names, relative, public_name
    ):
        if self._root_fd is None:
            raise OSError("library root is unavailable")
        parent_fd = parent_chain[-1]
        temporary_name = os.fsencode(
            f".qobuz-ownership-{self._nonce[:16]}-{secrets.token_hex(16)}"
        )
        public_relative = os.fsdecode(os.fsencode(os.sep).join(relative))
        temporary_relative = os.fsdecode(os.fsencode(os.sep).join([
            *relative[:-1], temporary_name
        ]))
        if not self._parent_chain_is_named_locked(
            parent_chain, parent_names
        ):
            raise OSError("ownership parent chain changed")
        os.mkdir(temporary_name, mode=0o777, dir_fd=parent_fd)
        child = _open_ownership_dir(temporary_name, dir_fd=parent_fd)
        created_stat = os.stat(
            temporary_name, dir_fd=parent_fd, follow_symlinks=False
        )
        held_stat = os.fstat(child)
        if (
            not stat.S_ISDIR(created_stat.st_mode)
            or int(created_stat.st_dev) != int(held_stat.st_dev)
            or int(created_stat.st_ino) != int(held_stat.st_ino)
        ):
            os.close(child)
            raise OSError("created ownership directory changed")

        record = {
            "fd": child,
            "relative": public_relative,
            "temporary_relative": temporary_relative,
            "directory": _ownership_identity(held_stat),
            "marker": None,
            "published": False,
        }
        self._created[os.fsencode(public_relative)] = record
        self._write_locked(sealed=False, items=[])
        try:
            token = _ownership_marker_token(self._nonce, public_relative)
            nofollow = getattr(os, "O_NOFOLLOW", None)
            if nofollow is None:
                raise OSError("safe marker creation is unavailable")
            marker_fd = os.open(
                _OWNERSHIP_MARKER,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=child,
            )
            try:
                encoded = (token + "\n").encode("ascii")
                offset = 0
                while offset < len(encoded):
                    written = os.write(marker_fd, encoded[offset:])
                    if written <= 0:
                        raise OSError("ownership marker write failed")
                    offset += written
                os.fsync(marker_fd)
                record["marker"] = _ownership_identity(os.fstat(marker_fd))
            finally:
                os.close(marker_fd)
            os.fsync(child)
            record["directory"] = _ownership_identity(os.fstat(child))
            self._write_locked(sealed=False, items=[])

            if not self._parent_chain_is_named_locked(
                parent_chain, parent_names
            ):
                raise OSError("ownership parent chain changed")
            try:
                _rename_ownership_noreplace(
                    parent_fd, temporary_name, parent_fd, public_name
                )
            except FileExistsError:
                self._discard_created_at_parent_locked(
                    parent_fd, temporary_name, record
                )
                if not self._parent_chain_is_named_locked(
                    parent_chain, parent_names
                ):
                    raise OSError("ownership parent chain changed")
                existing = _open_ownership_dir(
                    public_name, dir_fd=parent_fd
                )
                if not self._parent_chain_is_named_locked(
                    parent_chain, parent_names
                ):
                    os.close(existing)
                    raise OSError("ownership parent chain changed")
                return existing

            named = os.stat(
                public_name, dir_fd=parent_fd, follow_symlinks=False
            )
            current = os.fstat(child)
            if (
                int(named.st_dev) != int(current.st_dev)
                or int(named.st_ino) != int(current.st_ino)
            ):
                raise OSError("published ownership directory changed")
            record["published"] = True
            record["directory"] = _ownership_identity(current)
            self._write_locked(sealed=False, items=[])
            os.fsync(parent_fd)
            if not self._parent_chain_is_named_locked(
                parent_chain, parent_names
            ):
                raise OSError("ownership parent chain changed")
            return os.dup(child)
        except Exception:
            record_name = (
                public_name
                if record.get("published") is True
                else temporary_name
            )
            self._discard_created_at_parent_locked(
                parent_fd, record_name, record
            )
            raise

    def _source_items_locked(self):
        selected = getattr(self, "_source_items", None)
        if isinstance(selected, list) and selected:
            return selected
        selected = getattr(self, "_source_item", None)
        return [selected] if selected is not None else []

    def _select_source_item_locked(self, item, source=None):
        source_path = None
        if source is not None:
            try:
                source_path = os.path.abspath(os.fsencode(source))
            except (TypeError, ValueError):
                return None
        if getattr(self, "_managed", False) and source_path is None:
            return None
        matches = [
            selected
            for selected in self._source_items_locked()
            if (
                selected.get("source_path") == source_path
                if source_path is not None
                else selected.get("item") is item
            )
        ]
        return matches[0] if len(matches) == 1 else None

    def _before_item_moved(self, item, source, destination):
        with self._lock:
            selected = self._select_source_item_locked(item, source)
            if not self._enabled or selected is None:
                if getattr(self, "_managed", False):
                    raise OSError("managed move is not owned")
                return
            selected["pending_move"] = None
            selected["destination"] = None
            selected["destination_identity"] = None
            selected["move_proven"] = False
            try:
                if not self._root_chain_is_named_locked():
                    raise OSError("library root changed before move")
                source_path = os.path.abspath(os.fsencode(source))
                destination_path = os.path.abspath(os.fsencode(destination))
                if source_path != selected["source_path"]:
                    raise ValueError("ownership source path changed")
                named_source = os.stat(
                    selected["source_name"],
                    dir_fd=selected["source_parent_fd"],
                    follow_symlinks=False,
                )
                held_source = os.fstat(selected["source_fd"])
                if (
                    not stat.S_ISREG(named_source.st_mode)
                    or _ownership_identity(named_source) != _ownership_identity(held_source)
                ):
                    raise ValueError("ownership source changed before move")
                relative = os.path.relpath(destination_path, self._root)
                parts = _ownership_relative_parts(os.fsdecode(relative))
                if not parts or parts[:-1] != selected["destination_parent"]:
                    raise ValueError("ownership destination changed")
                selected["pending_move"] = (source_path, destination_path)
            except (OSError, TypeError, ValueError):
                selected["pending_move"] = None
                if getattr(self, "_managed", False):
                    raise

    def _item_moved(self, item, source, destination):
        with self._lock:
            selected = self._select_source_item_locked(item, source)
            if not self._enabled or selected is None:
                if getattr(self, "_managed", False):
                    raise OSError("managed move result is not owned")
                return
            destination_fd = None
            try:
                if not self._root_chain_is_named_locked():
                    raise OSError("library root changed during move")
                transition = (
                    os.path.abspath(os.fsencode(source)),
                    os.path.abspath(os.fsencode(destination)),
                )
                if transition != selected["pending_move"]:
                    raise ValueError("ownership move event changed")
                relative = os.path.relpath(transition[1], self._root)
                _, destination_fd, destination_stat, _scope = (
                    self._hold_relative_leaf_locked(relative)
                )
                source_stat = os.fstat(selected["source_fd"])
                source_identity = _ownership_identity(source_stat)
                destination_identity = _ownership_identity(destination_stat)
                if (
                    destination_identity != source_identity
                    and not self._stable_regular_files_equal(
                        selected["source_fd"], destination_fd
                    )
                ):
                    raise ValueError("ownership move did not copy the source")

                # The byte proof above is against held descriptors. Confirm
                # that the destination descriptor stayed unchanged and that
                # the public no-follow path still names that exact file before
                # publishing its identity into the manifest.
                held_destination = os.fstat(destination_fd)
                if (
                    not stat.S_ISREG(held_destination.st_mode)
                    or _ownership_identity(held_destination)
                    != destination_identity
                ):
                    raise ValueError("ownership destination changed")
                current = self._open_relative_leaf_locked(relative)
                if (
                    current is None
                    or _ownership_identity(current[1])
                    != destination_identity
                    or not self._root_chain_is_named_locked()
                ):
                    raise ValueError("ownership destination path changed")
                selected["destination"] = transition[1]
                selected["destination_identity"] = destination_identity
                selected["move_proven"] = True
            except (OSError, TypeError, ValueError):
                selected["destination"] = None
                selected["destination_identity"] = None
                selected["move_proven"] = False
                if getattr(self, "_managed", False):
                    raise
            finally:
                if destination_fd is not None:
                    try:
                        os.close(destination_fd)
                    except OSError:
                        pass

    def _item_copied(self, item, source, destination):
        del destination
        with self._lock:
            selected = self._select_source_item_locked(item, source)
            if selected is None:
                if getattr(self, "_managed", False):
                    raise OSError("managed copy result is not owned")
                return
            try:
                same_source = (
                    os.path.abspath(os.fsencode(source))
                    == selected["source_path"]
                )
            except (TypeError, ValueError):
                same_source = False
            if same_source or selected["item"] is item:
                selected["destination"] = None
                selected["destination_identity"] = None
                selected["move_proven"] = False
                if getattr(self, "_managed", False):
                    raise OSError("managed import copied instead of moving")

    @staticmethod
    def _stable_regular_files_equal(source_fd, destination_fd):
        try:
            source_before = os.fstat(source_fd)
            destination_before = os.fstat(destination_fd)
            if (
                not stat.S_ISREG(source_before.st_mode)
                or not stat.S_ISREG(destination_before.st_mode)
                or source_before.st_size != destination_before.st_size
            ):
                return False
            source_identity = _ownership_identity(source_before)
            destination_identity = _ownership_identity(destination_before)
            offset = 0
            while offset < source_before.st_size:
                amount = min(
                    _COPY_COMPARE_CHUNK,
                    source_before.st_size - offset,
                )
                source_chunk = os.pread(source_fd, amount, offset)
                destination_chunk = os.pread(
                    destination_fd, amount, offset
                )
                if (
                    len(source_chunk) != amount
                    or source_chunk != destination_chunk
                ):
                    return False
                offset += amount
            source_after = os.fstat(source_fd)
            destination_after = os.fstat(destination_fd)
            return (
                stat.S_ISREG(source_after.st_mode)
                and stat.S_ISREG(destination_after.st_mode)
                and _ownership_identity(source_after) == source_identity
                and _ownership_identity(destination_after)
                == destination_identity
            )
        except (OSError, TypeError, ValueError):
            return False

    def _hold_relative_leaf_locked(
            self, relative, *, album_scope_parts=None,
            expected_leaf_identity=None):
        parts = _ownership_relative_parts(os.fsdecode(relative))
        if not parts or self._root_fd is None:
            raise OSError("unsafe ownership destination")
        scope_parts = None
        if album_scope_parts is not None:
            scope_parts = tuple(album_scope_parts)
            if (
                not scope_parts
                or len(scope_parts) >= len(parts)
                or tuple(parts[:len(scope_parts)]) != scope_parts
            ):
                raise OSError("unsafe ownership album scope")
        chain = [os.dup(self._root_fd)]
        names = []
        leaf_fd = None
        try:
            for part in parts[:-1]:
                child = _open_ownership_dir(part, dir_fd=chain[-1])
                chain.append(child)
                names.append(part)
            if not self._parent_chain_is_named_locked(chain, names):
                raise OSError("ownership destination parent changed")
            named_before = os.stat(
                parts[-1],
                dir_fd=chain[-1],
                follow_symlinks=False,
            )
            if not stat.S_ISREG(named_before.st_mode):
                raise OSError("ownership destination is not a regular file")
            nofollow = getattr(os, "O_NOFOLLOW", None)
            if nofollow is None:
                raise OSError("safe destination access is unavailable")
            flags = (
                os.O_RDONLY
                | nofollow
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            leaf_fd = os.open(parts[-1], flags, dir_fd=chain[-1])
            held = os.fstat(leaf_fd)
            named_after = os.stat(
                parts[-1],
                dir_fd=chain[-1],
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(held.st_mode)
                or not stat.S_ISREG(named_after.st_mode)
                or _ownership_identity(named_before)
                != _ownership_identity(held)
                or _ownership_identity(named_after)
                != _ownership_identity(held)
                or (
                    expected_leaf_identity is not None
                    and _ownership_identity(held)
                    != expected_leaf_identity
                )
                or not self._parent_chain_is_named_locked(chain, names)
            ):
                raise OSError("ownership destination path changed")
            album_scope = None
            if scope_parts is not None:
                scope_length = len(scope_parts)
                ancestors = []
                for length in range(1, scope_length):
                    ancestors.append({
                        "relative": os.fsdecode(os.fsencode(os.sep).join(
                            scope_parts[:length]
                        )),
                        "directory": _ownership_identity(
                            os.fstat(chain[length])
                        ),
                    })
                album_scope = {
                    "relative": os.fsdecode(
                        os.fsencode(os.sep).join(scope_parts)
                    ),
                    "directory": _ownership_identity(
                        os.fstat(chain[scope_length])
                    ),
                    "ancestors": ancestors,
                }
                if not self._parent_chain_is_named_locked(chain, names):
                    raise OSError("ownership album scope changed")
            result = (parts, leaf_fd, held, album_scope)
            leaf_fd = None
            return result
        finally:
            if leaf_fd is not None:
                try:
                    os.close(leaf_fd)
                except OSError:
                    pass
            for descriptor in reversed(chain):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _open_relative_leaf_locked(self, relative):
        try:
            parts, leaf_fd, leaf, _scope = (
                self._hold_relative_leaf_locked(relative)
            )
        except (OSError, TypeError, ValueError):
            return None
        try:
            return parts, leaf
        finally:
            os.close(leaf_fd)

    def _created_for_item_locked(self, parent_parts):
        records = []
        for relative, record in self._created.items():
            if record.get("published") is not True:
                continue
            parts = _ownership_relative_parts(os.fsdecode(relative))
            if not parts or parent_parts[:len(parts)] != parts:
                continue
            try:
                current = self._open_created_locked(parts)
                persistent = os.fstat(record["fd"])
                if _ownership_identity(current) != _ownership_identity(persistent):
                    continue
                records.append({
                    "relative": os.fsdecode(relative),
                    **_ownership_identity(current),
                })
            except (OSError, TypeError, ValueError):
                continue
        return sorted(
            records,
            key=lambda entry: len(_ownership_relative_parts(entry["relative"]) or ()),
        )

    def _open_created_locked(self, parts):
        if self._root_fd is None:
            raise OSError("library root is unavailable")
        current = os.dup(self._root_fd)
        try:
            for part in parts:
                child = _open_ownership_dir(part, dir_fd=current)
                closing = current
                current = child
                os.close(closing)
            return os.fstat(current)
        finally:
            os.close(current)

    def _materialise_items_locked(self, lib):
        records = []
        companion_tasks = set()
        for selected in self._source_items_locked():
            if selected.get("move_proven") is not True:
                return []
            try:
                item_id = int(selected["item"].id)
            except (AttributeError, TypeError, ValueError):
                return []
            item = lib.get_item(item_id)
            if item is None:
                return []
            path = os.path.abspath(os.fsencode(item.path))
            if path != selected.get("destination"):
                return []
            try:
                relative = os.path.relpath(path, self._root)
            except (TypeError, ValueError):
                return []
            leaf_fd = None
            try:
                parts, leaf_fd, leaf, album_scope = (
                    self._hold_relative_leaf_locked(
                        relative,
                        album_scope_parts=selected.get("album_scope_parts"),
                        expected_leaf_identity=selected.get(
                            "destination_identity"
                        ),
                    )
                )
            except (OSError, TypeError, ValueError):
                return []
            try:
                if album_scope is None:
                    return []
                created_directories = self._created_for_item_locked(
                    parts[:-1])
                companions = []
                task = selected.get("task")
                task_key = id(task)
                module = _art_guard_module()
                if module is not None and task_key not in companion_tasks:
                    receipt = module.ownership_art_receipt(task)
                    if isinstance(receipt, dict):
                        companions.append(receipt)
                    companion_tasks.add(task_key)
                if getattr(self, "_managed", False):
                    declaration = selected.get("managed_declaration")
                    if declaration is None:
                        return []
                    records.append({
                        "slot": declaration["slot"],
                        "source": {
                            "path": declaration["path"],
                            "identity": list(declaration["identity"]),
                        },
                        "destination": {
                            "path": os.fsdecode(
                                os.fsencode(os.sep).join(parts)),
                            "identity": _ownership_identity(leaf),
                        },
                    })
                else:
                    records.append({
                        "relative": os.fsdecode(
                            os.fsencode(os.sep).join(parts)),
                        "file": _ownership_identity(leaf),
                        "album_scope": album_scope,
                        "created_directories": created_directories,
                        "companions": companions,
                    })
            finally:
                os.close(leaf_fd)
        return records

    def _discard_created_locked(self, key, record):
        if self._created.get(key) is record:
            self._created.pop(key, None)
        try:
            os.close(record["fd"])
        except OSError:
            pass

    def _remove_record_marker_locked(self, record):
        marker = record.get("marker")
        if marker is None:
            return True
        try:
            held = os.fstat(record["fd"])
            if not stat.S_ISDIR(held.st_mode):
                return False
            token = _ownership_marker_token(self._nonce, record["relative"])
            if not _remove_exact_ownership_marker(
                record["fd"], marker, token
            ):
                return False
            record["marker"] = None
            record["directory"] = _ownership_identity(
                os.fstat(record["fd"])
            )
            return True
        except (OSError, TypeError, ValueError):
            return False

    def _reclaim_record_locked(self, key, record, *, strip_marker):
        if self._root_fd is None:
            return False
        try:
            record["directory"] = _ownership_identity(
                os.fstat(record["fd"])
            )
            removed = _reclaim_ownership_directories(
                self._root_fd,
                [self._serialise_created_record(record)],
                self._nonce,
            )
            if removed == 1:
                self._discard_created_locked(key, record)
                return True
            if strip_marker and self._remove_record_marker_locked(record):
                record["directory"] = _ownership_identity(
                    os.fstat(record["fd"])
                )
                removed = _reclaim_ownership_directories(
                    self._root_fd,
                    [self._serialise_created_record(record)],
                    self._nonce,
                )
                if removed == 1:
                    self._discard_created_locked(key, record)
                    return True
        except (OSError, TypeError, ValueError):
            pass
        return False

    def _cleanup_unpublished_locked(self):
        for key, record in list(self._created.items()):
            if record.get("published") is not True:
                self._reclaim_record_locked(
                    key, record, strip_marker=True
                )

    def _remove_markers_locked(self):
        if self._root_fd is None:
            return False
        self._cleanup_unpublished_locked()
        for record in list(self._created.values()):
            if record.get("published") is not True:
                continue
            if not self._remove_record_marker_locked(record):
                return False
            self._write_locked(sealed=False, items=[])
        return True

    def _seal(self, lib):
        with self._lock:
            try:
                if not self._enabled or self._root_fd is None:
                    if getattr(self, "_managed", False):
                        raise OSError("managed ownership gate did not start")
                    return
                if not self._root_chain_is_named_locked():
                    if getattr(self, "_managed", False):
                        raise OSError("managed library root changed")
                    return
                root_stat = os.fstat(self._root_fd)
                if (
                    int(root_stat.st_dev),
                    int(root_stat.st_ino),
                ) != self._root_device_inode:
                    if getattr(self, "_managed", False):
                        raise OSError("managed library root identity changed")
                    return
                if not self._materialise_items_locked(lib):
                    if getattr(self, "_managed", False):
                        raise OSError("managed import result is incomplete")
                    return
                if not self._remove_markers_locked():
                    if getattr(self, "_managed", False):
                        raise OSError("managed directory cleanup is incomplete")
                    return
                items = self._materialise_items_locked(lib)
                if not items:
                    if getattr(self, "_managed", False):
                        raise OSError("managed import has no proven results")
                    return
                if (
                    getattr(self, "_managed", False)
                    and len(items) != len(self._managed_intent)
                ):
                    raise OSError("managed import result count changed")
                if not self._root_chain_is_named_locked():
                    if getattr(self, "_managed", False):
                        raise OSError("managed library root changed")
                    return
                self._write_locked(sealed=True, items=items)
                self._preserve_created = True
            except (OSError, TypeError, ValueError):
                if getattr(self, "_managed", False):
                    raise
                return
            finally:
                self._close_locked()

    @staticmethod
    def _serialise_created_record(record):
        return {
            "relative": record["relative"],
            "temporary_relative": record["temporary_relative"],
            "directory": record["directory"],
            "marker": record.get("marker"),
        }

    def _created_payload_locked(self):
        payload = []
        for record in self._created.values():
            try:
                record["directory"] = _ownership_identity(os.fstat(record["fd"]))
            except OSError:
                pass
            payload.append(self._serialise_created_record(record))
        return payload

    def _write_locked(self, *, sealed, items):
        if self._fd is None or self._root is None or self._root_fd is None:
            return
        if not self._root_chain_is_named_locked():
            raise OSError("library root changed while writing ownership evidence")
        if getattr(self, "_managed", False):
            self._managed_generation += 1
            payload = {
                "version": _MANAGED_SNAPSHOT_VERSION,
                "generation": self._managed_generation,
                "previous_hash": self._managed_previous_hash,
                "nonce": self._nonce,
                "owner": self._managed_owner,
                "root": os.fsdecode(self._managed_root),
                "root_identity": self._managed_root_identity,
                "sealed": sealed is True,
                "intent": list(self._managed_intent),
                "mappings": items,
                "cleanup_directories": self._created_payload_locked(),
            }
            self._managed_previous_hash = _write_managed_snapshot(
                self._fd, payload)
            return
        payload = {
            "version": 1,
            "nonce": self._nonce,
            "root": os.fsdecode(self._root),
            "root_identity": _ownership_identity(os.fstat(self._root_fd)),
            "sealed": sealed is True,
            "items": items,
            "cleanup_directories": self._created_payload_locked(),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n"
        current_size = os.fstat(self._fd).st_size
        if current_size + len(encoded) > _MAX_MANIFEST_BYTES:
            raise OSError("ownership manifest is too large")
        offset = 0
        while offset < len(encoded):
            written = os.pwrite(
                self._fd, encoded[offset:], current_size + offset
            )
            if written <= 0:
                raise OSError("ownership manifest write failed")
            offset += written
        os.fsync(self._fd)

    def _close_locked(self):
        self._enabled = False
        if self._root_fd is not None:
            if not self._preserve_created:
                try:
                    self._write_locked(sealed=False, items=[])
                except (OSError, TypeError, ValueError):
                    pass
            for key, record in list(self._created.items()):
                if (
                    self._preserve_created
                    and record.get("published") is True
                ):
                    continue
                self._reclaim_record_locked(
                    key, record, strip_marker=True
                )
            if not self._preserve_created:
                try:
                    self._write_locked(sealed=False, items=[])
                except (OSError, TypeError, ValueError):
                    pass

        source_items = self._source_items_locked()
        module = _art_guard_module()
        released_tasks = set()
        for selected in source_items:
            task = selected.get("task")
            task_key = id(task)
            if module is not None and task_key not in released_tasks:
                module.release_ownership_art(task)
                released_tasks.add(task_key)
            for key in ("source_fd", "source_parent_fd"):
                try:
                    os.close(selected[key])
                except OSError:
                    pass
        self._source_item = None
        self._source_items = []
        for record in self._created.values():
            try:
                os.close(record["fd"])
            except OSError:
                pass
        self._created.clear()
        root_chain = getattr(self, "_root_chain", ())
        if root_chain:
            for descriptor in reversed(root_chain):
                try:
                    os.close(descriptor)
                except BaseException:
                    pass
        elif self._root_fd is not None:
            try:
                os.close(self._root_fd)
            except BaseException:
                pass
        self._root_chain = ()
        self._root_names = ()
        self._root_fd = None
