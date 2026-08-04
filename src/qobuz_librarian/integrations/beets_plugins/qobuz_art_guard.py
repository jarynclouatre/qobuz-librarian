"""Fail-closed artwork publication for application-managed Beets imports."""

from __future__ import annotations

import os
import secrets
import stat
import threading

from beets import plugins
from beets.plugins import BeetsPlugin

from qobuz_librarian.integrations.beets import (
    _merge_chain_is_named,
    _merge_name_matches,
    _merge_name_missing,
    _open_exact_merge_dir,
    _open_ownership_dir,
    _ownership_identity,
    _ownership_relative_parts,
    _rename_ownership_noreplace,
    _rollback_exact_merge_entry,
)

_STATE_ATTRIBUTE = "_qobuz_librarian_art_guard"
_MANIFEST_FD_ENV = "QOBUZ_LIBRARIAN_OWNERSHIP_FD"
_NONCE_ENV = "QOBUZ_LIBRARIAN_OWNERSHIP_NONCE"


def _ownership_requested():
    try:
        descriptor = int(os.environ.get(_MANIFEST_FD_ENV, ""))
    except (TypeError, ValueError):
        return False
    nonce = os.environ.get(_NONCE_ENV, "")
    return (
        descriptor >= 3
        and len(nonce) == 64
        and all(char in "0123456789abcdef" for char in nonce)
    )


def _state_for(task):
    state = getattr(task, _STATE_ATTRIBUTE, None)
    return state if isinstance(state, dict) and state.get("version") == 1 else None


def _remove_state(task):
    try:
        delattr(task, _STATE_ATTRIBUTE)
    except AttributeError:
        pass


def _name_is_missing(parent_fd, name):
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _leaf_is_absent(parent_fd, name):
    return _name_is_missing(parent_fd, name)


def _close_chain(chain):
    if isinstance(chain, list):
        for descriptor in reversed(chain):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _open_relative_directory(root_fd, parts):
    current = os.dup(root_fd)
    try:
        for part in parts:
            child = _open_ownership_dir(part, dir_fd=current)
            closing = current
            current = child
            os.close(closing)
        return current
    except BaseException:
        os.close(current)
        raise


def _same_directory(first_fd, second_fd):
    try:
        first = os.fstat(first_fd)
        second = os.fstat(second_fd)
    except OSError:
        return False
    return (
        stat.S_ISDIR(first.st_mode)
        and stat.S_ISDIR(second.st_mode)
        and (int(first.st_dev), int(first.st_ino))
        == (int(second.st_dev), int(second.st_ino))
    )


def _parent_still_held(state):
    parts = state.get("parent_parts")
    parent_fd = state.get("parent_fd")
    if not isinstance(parts, tuple) or type(parent_fd) is not int:
        return False
    chain = state.get("chain")
    if isinstance(chain, list):
        return (
            chain
            and chain[-1] == parent_fd
            and _root_still_named(state, chain[0])
            and _merge_chain_is_named(chain, parts)
        )
    root_fd = state.get("root_fd")
    if type(root_fd) is not int:
        return False
    opened = None
    try:
        if not _root_still_named(state, root_fd):
            return False
        opened = _open_relative_directory(root_fd, parts)
        return _same_directory(opened, parent_fd)
    except OSError:
        return False
    finally:
        if opened is not None:
            os.close(opened)


def _root_still_named(state, root_fd):
    root = state.get("root")
    if root is None or type(root_fd) is not int:
        return False
    try:
        named = os.stat(root, follow_symlinks=False)
        held = os.fstat(root_fd)
    except (OSError, TypeError, ValueError):
        return False
    return (
        stat.S_ISDIR(named.st_mode)
        and stat.S_ISDIR(held.st_mode)
        and _ownership_identity(named) == _ownership_identity(held)
    )


def _hold_existing_prefix(root, parts):
    chain = [_open_ownership_dir(root)]
    names = []
    try:
        for part in parts:
            try:
                child = _open_exact_merge_dir(chain[-1], part)
            except FileNotFoundError:
                break
            chain.append(child)
            names.append(part)
        return chain, tuple(names)
    except BaseException:
        _close_chain(chain)
        raise


def _hold_task_item_in_parent(state, item):
    parent_fd = state.get("parent_fd")
    destination = state.get("destination")
    if (
        item is None
        or type(parent_fd) is not int
        or destination is None
        or type(state.get("item_fd")) is int
    ):
        return False
    expected_parent = os.path.dirname(destination)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        return False
    descriptor = None
    try:
        path = os.path.abspath(os.fsencode(item.path))
        if os.path.dirname(path) != expected_parent:
            return False
        name = os.path.basename(path)
        if not name:
            return False
        descriptor = os.open(
            name,
            os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        held = os.fstat(descriptor)
        if (
            stat.S_ISREG(held.st_mode)
            and _merge_name_matches(
                parent_fd, name, descriptor, regular=True
            )
        ):
            state["item_fd"] = descriptor
            state["item_name"] = name
            state["item_identity"] = _ownership_identity(held)
            descriptor = None
            return True
        return False
    except (AttributeError, OSError, TypeError, ValueError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _task_item_is_in_parent(state):
    task = state.get("task")
    if task is None:
        return False
    try:
        items = list(task.imported_items())
    except (AttributeError, TypeError, ValueError):
        return False
    return any(_hold_task_item_in_parent(state, item) for item in items)


def _held_task_item_is_still_named(state, *, allow_metadata_change=False):
    parent_fd = state.get("parent_fd")
    item_fd = state.get("item_fd")
    item_name = state.get("item_name")
    expected = state.get("item_identity")
    if (
        type(parent_fd) is not int
        or type(item_fd) is not int
        or not isinstance(item_name, bytes)
        or not isinstance(expected, dict)
    ):
        return False
    try:
        held = os.fstat(item_fd)
        current = _ownership_identity(held)
        if (
            not stat.S_ISREG(held.st_mode)
            or not _merge_name_matches(
                parent_fd, item_name, item_fd, regular=True
            )
        ):
            return False
        if allow_metadata_change:
            if (
                current["device"],
                current["inode"],
            ) != (
                expected.get("device"),
                expected.get("inode"),
            ):
                return False
            state["item_identity"] = current
            return True
        return current == expected
    except (OSError, TypeError, ValueError):
        return False


def _authorise_deferred_parent(state):
    if (
        state.get("ownership") is True
        or state.get("parent_was_absent") is not True
    ):
        return False
    chain = state.get("chain")
    prefix_parts = state.get("prefix_parts")
    parent_parts = state.get("parent_parts")
    if (
        not isinstance(chain, list)
        or not isinstance(prefix_parts, tuple)
        or not isinstance(parent_parts, tuple)
        or len(chain) != len(prefix_parts) + 1
        or parent_parts[:len(prefix_parts)] != prefix_parts
        or not _root_still_named(state, chain[0])
        or not _merge_chain_is_named(chain, prefix_parts)
    ):
        return False
    try:
        for part in parent_parts[len(prefix_parts):]:
            chain.append(_open_exact_merge_dir(chain[-1], part))
        state["parent_fd"] = chain[-1]
        if (
            not _parent_still_held(state)
            or not _leaf_is_absent(chain[-1], state["leaf"])
            or not _task_item_is_in_parent(state)
        ):
            return False
        state["authorised"] = True
        return True
    except (OSError, TypeError, ValueError):
        return False


def _art_destination(root, task, candidate):
    items = list(task.imported_items())
    if not items or getattr(candidate, "path", None) is None:
        raise ValueError("artwork has no exact imported destination")

    def item_order(item):
        try:
            return 0, int(item.id)
        except (AttributeError, TypeError, ValueError):
            return 1, items.index(item)

    item = min(items, key=item_order)
    item_directory = os.path.dirname(
        os.path.abspath(os.fsencode(item.destination()))
    )
    destination = os.path.abspath(os.fsencode(
        task.album.art_destination(candidate.path, item_dir=item_directory)
    ))
    if os.path.dirname(destination) != item_directory:
        raise ValueError("artwork destination leaves the exact item directory")
    relative = os.path.relpath(destination, root)
    parts = _ownership_relative_parts(os.fsdecode(relative))
    if not parts:
        raise ValueError("unsafe artwork destination")
    return destination, tuple(parts[:-1]), parts[-1]


def _exact_fetchart(task):
    for plugin in plugins.find_plugins():
        candidates = getattr(plugin, "art_candidates", None)
        if (
            getattr(plugin, "name", None) == "fetchart"
            and isinstance(candidates, dict)
            and task in candidates
        ):
            return plugin, candidates.pop(task)
    return None, None


def _same_art_path(left, right):
    if left is None or right is None:
        return left is None and right is None
    try:
        return os.path.abspath(os.fsencode(left)) == os.path.abspath(
            os.fsencode(right)
        )
    except (OSError, TypeError, ValueError):
        return False


def _album_store_rollback_is_proved(album, old_art):
    try:
        fresh = album.get_fresh_from_db()
        current_art = getattr(fresh, "artpath", None)
    except BaseException:
        return False
    return _same_art_path(current_art, old_art)


def _exact_file_layout(parent_fd, public_name, private_name, held_fd):
    public_matches = _merge_name_matches(
        parent_fd, public_name, held_fd, regular=True)
    private_matches = _merge_name_matches(
        parent_fd, private_name, held_fd, regular=True)
    public_missing = _merge_name_missing(parent_fd, public_name)
    private_missing = _merge_name_missing(parent_fd, private_name)
    if public_matches and private_missing:
        return "public"
    if private_matches and public_missing:
        return "private"
    if public_missing and private_missing:
        return "removed"
    return "uncertain"


def _restore_exact_file(parent_fd, public_name, private_name, held_fd):
    layout = _exact_file_layout(
        parent_fd, public_name, private_name, held_fd)
    if layout == "public":
        return True
    if layout != "private":
        return False
    try:
        _rollback_exact_merge_entry(
            parent_fd, public_name, parent_fd, private_name, held_fd)
    except BaseException:
        pass
    if _exact_file_layout(
            parent_fd, public_name, private_name, held_fd) != "public":
        return False
    try:
        os.fsync(parent_fd)
    except BaseException:
        return False
    return True


def _open_exact_entry(parent_fd, name):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    path_only = getattr(os, "O_PATH", None)
    if nofollow is None or path_only is None:
        raise OSError("safe exact entry access is unavailable")
    descriptor = os.open(
        name,
        path_only | nofollow | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    try:
        if not _merge_name_matches(parent_fd, name, descriptor):
            raise OSError("private artwork entry changed while it was opened")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _restore_unexpected_private_file(
        parent_fd, public_name, private_name, held_fd):
    """Put back an unexpected entry moved by a raced path-based rename."""
    if (
        not _merge_name_missing(parent_fd, public_name)
        or _merge_name_missing(parent_fd, private_name)
    ):
        return False
    unexpected_fd = None
    try:
        unexpected_fd = _open_exact_entry(parent_fd, private_name)
        if _merge_name_matches(parent_fd, private_name, held_fd):
            return False
        deferred = None
        try:
            _rename_ownership_noreplace(
                parent_fd, private_name, parent_fd, public_name)
        except BaseException as exc:
            deferred = exc
        restored = (
            _merge_name_matches(parent_fd, public_name, unexpected_fd)
            and _merge_name_missing(parent_fd, private_name)
        )
        if restored:
            try:
                os.fsync(parent_fd)
            except BaseException as exc:
                if deferred is None:
                    deferred = exc
                restored = False
        if deferred is not None and not isinstance(deferred, OSError):
            if not restored and hasattr(deferred, "add_note"):
                deferred.add_note(
                    "the unexpected artwork entry could not be restored")
            raise deferred
        return restored
    except OSError:
        return False
    finally:
        if unexpected_fd is not None:
            os.close(unexpected_fd)


def _remove_exact_file(parent_fd, name, held_fd):
    quarantine = os.fsencode(f".qobuz-art-cleanup-{secrets.token_hex(16)}")
    deferred = None
    try:
        if _exact_file_layout(
                parent_fd, name, quarantine, held_fd) != "public":
            return False
        try:
            _rename_ownership_noreplace(
                parent_fd, name, parent_fd, quarantine)
        except BaseException as exc:
            if _exact_file_layout(
                    parent_fd, name, quarantine, held_fd) != "private":
                _restore_unexpected_private_file(
                    parent_fd, name, quarantine, held_fd)
                if isinstance(exc, OSError):
                    return False
                raise
            deferred = exc

        if _exact_file_layout(
                parent_fd, name, quarantine, held_fd) != "private":
            _restore_unexpected_private_file(
                parent_fd, name, quarantine, held_fd)
            return False
        try:
            os.fsync(parent_fd)
        except BaseException as exc:
            restored = _restore_exact_file(
                parent_fd, name, quarantine, held_fd)
            error = deferred or exc
            if not restored and hasattr(error, "add_note"):
                error.add_note("the exact artwork cleanup could not be restored")
            if not isinstance(error, OSError):
                raise error
            return False

        unlink_error = None
        try:
            os.unlink(quarantine, dir_fd=parent_fd)
        except BaseException as exc:
            layout = _exact_file_layout(
                parent_fd, name, quarantine, held_fd)
            if layout != "removed":
                restored = _restore_exact_file(
                    parent_fd, name, quarantine, held_fd)
                error = deferred or exc
                if not restored and hasattr(error, "add_note"):
                    error.add_note(
                        "the exact artwork cleanup could not be restored")
                if not isinstance(error, OSError):
                    raise error
                return False
            unlink_error = exc

        sync_error = None
        try:
            os.fsync(parent_fd)
        except BaseException as exc:
            sync_error = exc
        removed = _exact_file_layout(
            parent_fd, name, quarantine, held_fd) == "removed"
        error = deferred or unlink_error or sync_error
        if error is not None and not isinstance(error, OSError):
            raise error
        return removed and sync_error is None
    except BaseException:
        layout = _exact_file_layout(
            parent_fd, name, quarantine, held_fd)
        if layout == "private":
            _restore_exact_file(parent_fd, name, quarantine, held_fd)
        elif layout == "uncertain":
            _restore_unexpected_private_file(
                parent_fd, name, quarantine, held_fd)
        raise


def _open_candidate(candidate):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("safe artwork source access is unavailable")
    descriptor = os.open(
        candidate.path,
        os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise OSError("artwork source is not a regular file")
    return descriptor


def _copy_candidate_to_private(parent_fd, candidate_fd):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("safe artwork publication is unavailable")
    temporary = os.fsencode(f".qobuz-art-{secrets.token_hex(16)}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
        | getattr(os, "O_CLOEXEC", 0),
        0o644,
        dir_fd=parent_fd,
    )
    try:
        while True:
            block = os.read(candidate_fd, 1024 * 1024)
            if not block:
                break
            offset = 0
            while offset < len(block):
                written = os.write(descriptor, block[offset:])
                if written <= 0:
                    raise OSError("artwork write failed")
                offset += written
        os.fsync(descriptor)
        if not _merge_name_matches(
            parent_fd, temporary, descriptor, regular=True
        ):
            raise OSError("private artwork changed before publication")
        return temporary, descriptor
    except BaseException:
        _remove_exact_file(parent_fd, temporary, descriptor)
        os.close(descriptor)
        raise


def _candidate_within_task(task, candidate_path):
    top = getattr(task, "toppath", None)
    if top is None:
        return None
    try:
        top = os.path.abspath(os.fsencode(top))
        path = os.path.abspath(os.fsencode(candidate_path))
        relative = os.path.relpath(path, top)
        parts = _ownership_relative_parts(os.fsdecode(relative))
        if not parts:
            return None
        return top, parts
    except (OSError, TypeError, ValueError):
        return None


def _remove_candidate_source(task, candidate, held_fd):
    within = _candidate_within_task(task, candidate.path)
    if within is None:
        return False
    top, parts = within
    parent_fd = None
    try:
        parent_fd = _open_ownership_dir(top)
        for part in parts[:-1]:
            child = _open_ownership_dir(part, dir_fd=parent_fd)
            closing = parent_fd
            parent_fd = child
            os.close(closing)
        return _remove_exact_file(parent_fd, parts[-1], held_fd)
    except OSError:
        return False
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _capture_art(state):
    if state.get("published") is not True or not _parent_still_held(state):
        return None
    parent_fd = state["parent_fd"]
    leaf = state["leaf"]
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        return None
    descriptor = None
    try:
        descriptor = os.open(
            leaf,
            os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or not _merge_name_matches(
                parent_fd, leaf, descriptor, regular=True
            )
            or _ownership_identity(current) != state.get("published_identity")
        ):
            return None
        album_art = getattr(state["task"].album, "artpath", None)
        if (
            album_art is None
            or os.path.abspath(os.fsencode(album_art)) != state["destination"]
        ):
            return None
        relative = os.fsdecode(os.fsencode(os.sep).join([
            *state["parent_parts"], leaf
        ]))
        return {
            "kind": "artwork",
            "relative": relative,
            "file": _ownership_identity(current),
        }
    except (OSError, TypeError, ValueError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def authorise_ownership_art(task, root_fd, created_record, selected_item):
    """Allow one selected staged item in the exact created parent."""
    state = _state_for(task)
    if (
        state is None
        or state.get("ownership") is not True
        or state.get("authorised") is True
        or not isinstance(created_record, dict)
        or created_record.get("published") is not True
        or type(root_fd) is not int
        or selected_item is None
    ):
        return False
    record_fd = created_record.get("fd")
    record_relative = created_record.get("relative")
    parts = _ownership_relative_parts(record_relative)
    if (
        type(record_fd) is not int
        or not parts
        or tuple(parts) != state.get("parent_parts")
    ):
        return False
    opened = None
    try:
        opened = _open_relative_directory(root_fd, parts)
        if (
            not _same_directory(opened, record_fd)
            or not _leaf_is_absent(record_fd, state["leaf"])
        ):
            return False
        state["root_fd"] = root_fd
        state["parent_fd"] = record_fd
        state["selected_item"] = selected_item
        state["authorised"] = True
        return True
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if opened is not None:
            os.close(opened)


def ownership_art_receipt(task):
    """Return one exact typed receipt, revalidated at ownership seal time."""
    state = _state_for(task)
    if state is None or state.get("ownership") is not True:
        return None
    receipt = _capture_art(state)
    return dict(receipt) if receipt is not None else None


def release_ownership_art(task):
    state = _state_for(task)
    if state is not None and state.get("ownership") is True:
        _remove_state(task)


class QobuzArtGuardPlugin(BeetsPlugin):
    """Publish fetched art only into an exact directory this run created."""

    def __init__(self):
        super().__init__()
        self._lock = threading.RLock()
        self.import_stages = [self._guard_art]
        self.register_listener("import_task_files", self._publish_art)

    def _guard_art(self, session, task):
        with self._lock:
            fetchart, candidate = _exact_fetchart(task)
            if fetchart is None or candidate is None:
                return
            state = {
                "version": 1,
                "task": task,
                "fetchart": fetchart,
                "candidate": candidate,
                "ownership": _ownership_requested(),
                "authorised": False,
                "published": False,
                "chain": None,
            }
            try:
                root = os.path.abspath(os.fsencode(session.lib.directory))
                destination, parent_parts, leaf = _art_destination(
                    root, task, candidate
                )
                state.update({
                    "root": root,
                    "destination": destination,
                    "parent_parts": parent_parts,
                    "leaf": leaf,
                })
                setattr(task, _STATE_ATTRIBUTE, state)
                if state["ownership"]:
                    return

                chain, prefix_parts = _hold_existing_prefix(
                    root, parent_parts
                )
                state.update({
                    "chain": chain,
                    "prefix_parts": prefix_parts,
                    "parent_was_absent": len(prefix_parts) < len(parent_parts),
                })
            except (OSError, TypeError, ValueError):
                return

    def _publish_candidate(self, state):
        if (
            state.get("authorised") is not True
            or not _parent_still_held(state)
            or not _leaf_is_absent(state["parent_fd"], state["leaf"])
            or not _held_task_item_is_still_named(state)
        ):
            return
        task = state["task"]
        candidate = state["candidate"]
        fetchart = state["fetchart"]
        if getattr(task.album, "artpath", None):
            return

        candidate_fd = None
        temporary_fd = None
        destination_fd = None
        temporary = None
        published = False
        stored = False
        old_art = getattr(task.album, "artpath", None)
        try:
            candidate_fd = _open_candidate(candidate)
            source_identity = _ownership_identity(os.fstat(candidate_fd))
            temporary, temporary_fd = _copy_candidate_to_private(
                state["parent_fd"], candidate_fd
            )
            if _ownership_identity(os.fstat(candidate_fd)) != source_identity:
                raise OSError("artwork source changed while it was copied")
            if (
                not _parent_still_held(state)
                or not _leaf_is_absent(state["parent_fd"], state["leaf"])
                or not _held_task_item_is_still_named(state)
            ):
                raise OSError("artwork destination changed while it was copied")
            try:
                _rename_ownership_noreplace(
                    state["parent_fd"],
                    temporary,
                    state["parent_fd"],
                    state["leaf"],
                )
            except BaseException:
                if _exact_file_layout(
                        state["parent_fd"],
                        state["leaf"],
                        temporary,
                        temporary_fd,
                ) == "public":
                    published = True
                    temporary = None
                raise
            published = True
            temporary = None
            if not _merge_name_matches(
                state["parent_fd"],
                state["leaf"],
                temporary_fd,
                regular=True,
            ):
                raise OSError("published artwork changed")
            os.fsync(state["parent_fd"])
            destination_fd = os.dup(temporary_fd)
            if not _parent_still_held(state):
                raise OSError("artwork destination changed during publication")
            state["published_identity"] = _ownership_identity(
                os.fstat(destination_fd)
            )
            state["published"] = True
            task.album.artpath = state["destination"]
            plugins.send("art_set", album=task.album)
            if (
                not _parent_still_held(state)
                or not _held_task_item_is_still_named(
                    state, allow_metadata_change=True
                )
            ):
                raise OSError("artwork destination changed before storage")
            if getattr(fetchart, "store_source", False):
                task.album.art_source = candidate.source_name
            try:
                task.album.store()
            except BaseException:
                if not _album_store_rollback_is_proved(
                        task.album, old_art):
                    stored = True
                raise
            stored = True

            try:
                removal_enabled = fetchart._is_source_file_removal_enabled()
                is_fallback = fetchart._is_candidate_fallback(candidate)
                if (
                    removal_enabled
                    and not is_fallback
                ):
                    _remove_candidate_source(task, candidate, candidate_fd)
            except Exception as exc:
                self._log.warning(
                    "qobuz_art_guard: artwork was stored, but its staging "
                    "cleanup failed: {0}",
                    exc,
                )
        except BaseException as exc:
            if not stored:
                published_fd = destination_fd or temporary_fd
                if published and published_fd is not None:
                    _remove_exact_file(
                        state["parent_fd"], state["leaf"], published_fd
                    )
                state["published"] = False
                task.album.artpath = old_art
            if not isinstance(exc, OSError):
                raise
            self._log.warning(
                "qobuz_art_guard: could not safely publish art for "
                "{0.albumartist} - {0.album}: {1}",
                task.album,
                exc,
            )
        finally:
            if temporary is not None and temporary_fd is not None:
                _remove_exact_file(
                    state["parent_fd"], temporary, temporary_fd
                )
            for descriptor in (destination_fd, temporary_fd, candidate_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

    def _publish_art(self, session, task):
        del session
        with self._lock:
            state = _state_for(task)
            if state is None:
                return
            try:
                if state.get("ownership") is True:
                    if not _hold_task_item_in_parent(
                        state, state.get("selected_item")
                    ):
                        return
                else:
                    _authorise_deferred_parent(state)
                self._publish_candidate(state)
            finally:
                item_fd = state.pop("item_fd", None)
                if type(item_fd) is int:
                    try:
                        os.close(item_fd)
                    except OSError:
                        pass
                chain = state.get("chain")
                if isinstance(chain, list):
                    _close_chain(chain)
                    state["chain"] = None
                    state.pop("parent_fd", None)
                if state.get("ownership") is not True:
                    _remove_state(task)
