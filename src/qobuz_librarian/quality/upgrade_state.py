"""Shared upgrade scan state."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library.artist_fingerprint import artist_fingerprint
from qobuz_librarian.library.catalog import album_year

STATE_VERSION = 1
_STATE_LOCK = threading.Lock()


@dataclass
class RefreshResult:
    candidates: list[dict]
    artists_scanned: list[str]
    errors: dict[str, str]
    complete: bool
    fingerprints: dict[str, str] = field(default_factory=dict)
    hidden_signature: str = ""
    refresh_started_at: float = 0.0


def _empty_state():
    return {
        "version": STATE_VERSION,
        "updated_at": None,
        "complete": False,
        "artists_scanned": [],
        "errors": {},
        "fingerprints": {},
        "artist_updated_at": {},
        "hidden_signature": "",
        "candidates": [],
    }


def _album_cover(album):
    img = album.get("image") or {}
    url = img.get("small") or img.get("thumbnail") or ""
    return url if url.startswith("https://static.qobuz.com/") else ""


def _candidate_spec(artist_name: str, candidate: dict):
    album = candidate["qobuz_album"]
    title = album.get("title") or "?"
    n_present = candidate.get("n_present", 0)
    n_total = candidate.get("n_total", 0)
    part = f" · {n_present}/{n_total} tracks" if n_total and n_present < n_total else ""
    return {
        "title": title,
        "artist": artist_name,
        "detail": (f"{candidate.get('existing_quality_label', '?')} → "
                   f"{candidate.get('target_quality_label', '?')}{part}"),
        "payload": {
            "album_id": album.get("id"),
            # Where the scan found the album locally, so a later walk can tell
            # a candidate that's still there from one whose folder has gone.
            "album_dir": str(candidate.get("album_dir") or ""),
            "year": album_year(album),
            "cover": _album_cover(album),
            "needed_edition_swap": bool(candidate.get("_needed_edition_swap")),
            "title_similarity": float(candidate.get("_title_similarity") or 0.0),
        },
    }


def _hidden_signature(hidden):
    if hidden is None:
        return ""
    bucket = hidden.get(hidden_mod.SCOPE_UPGRADE) if isinstance(hidden, dict) else {}
    if not isinstance(bucket, dict):
        bucket = {}
    return json.dumps(bucket, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"))


def load():
    data = state_file.load_json_object(
        cfg.UPGRADE_STATE_FILE, "the saved upgrade scan",
        "the Upgrade candidates from your last Library refresh")
    # A version the build doesn't know is a deliberate schema signal, not
    # corruption: leave the file alone and rebuild from a fresh scan.
    if data is None or data.get("version") != STATE_VERSION:
        return _empty_state()
    base = _empty_state()
    base.update({
        "updated_at": data.get("updated_at"),
        "complete": bool(data.get("complete")),
        "artists_scanned": list(data.get("artists_scanned") or []),
        "errors": data.get("errors") if isinstance(data.get("errors"), dict) else {},
        "fingerprints": (data.get("fingerprints")
                         if isinstance(data.get("fingerprints"), dict) else {}),
        "artist_updated_at": (data.get("artist_updated_at")
                              if isinstance(data.get("artist_updated_at"), dict)
                              else {}),
        "hidden_signature": str(data.get("hidden_signature") or ""),
        "candidates": (data.get("candidates")
                       if isinstance(data.get("candidates"), list) else []),
    })
    return base


def _write_state(data):
    try:
        state_file.write_json(cfg.UPGRADE_STATE_FILE, data)
    except OSError as e:
        # The Upgrade view reads this snapshot; a failed write means it shows
        # stale candidates until the next scan. Surface it (verbose) instead
        # of staying silent on a full/read-only volume.
        from qobuz_librarian.ui_cli.logging import vlog
        vlog(f"upgrade state write failed ({e}); saved upgrade view may be stale")


def _state_from_result(result: RefreshResult):
    now = time.time()
    return {
        "version": STATE_VERSION,
        "updated_at": now,
        "complete": bool(result.complete),
        "artists_scanned": list(result.artists_scanned),
        "errors": dict(result.errors),
        "fingerprints": dict(result.fingerprints),
        "artist_updated_at": {name: now for name in result.artists_scanned},
        "hidden_signature": result.hidden_signature,
        "candidates": list(result.candidates),
    }


def _preserve_concurrent_artist_updates(data, refresh_started_at):
    if not refresh_started_at:
        return data
    current = load()
    current_artist_updated_at = current.get("artist_updated_at") or {}
    current_fingerprints = current.get("fingerprints") or {}
    preserved_artists = {
        name for name, updated_at in current_artist_updated_at.items()
        if float(updated_at or 0) > float(refresh_started_at)
    }
    if not preserved_artists:
        return data
    data["candidates"] = [
        c for c in data.get("candidates") or []
        if c.get("artist") not in preserved_artists
    ] + [
        c for c in current.get("candidates") or []
        if c.get("artist") in preserved_artists
    ]
    data["artists_scanned"] = list(dict.fromkeys(
        list(data.get("artists_scanned") or [])
        + [name for name in current.get("artists_scanned") or []
           if name in preserved_artists]
    ))
    data_artist_updated_at = dict(data.get("artist_updated_at") or {})
    for name in preserved_artists:
        data["fingerprints"][name] = current_fingerprints.get(name, "")
        data_artist_updated_at[name] = current_artist_updated_at.get(name, 0)
    data["artist_updated_at"] = data_artist_updated_at
    return data


def save(
    result: RefreshResult,
    *,
    preserve_concurrent: bool = False,
    refresh_started_at=None,
):
    with _STATE_LOCK:
        data = _state_from_result(result)
        if preserve_concurrent:
            data = _preserve_concurrent_artist_updates(
                data,
                refresh_started_at
                if refresh_started_at is not None
                else result.refresh_started_at,
            )
        _write_state(data)


def _default_scan_artist(token, args, capped):
    from qobuz_librarian.quality.decision import scan_artist_for_upgrades

    def scan_artist(artist_dir):
        return scan_artist_for_upgrades(
            artist_dir.name, artist_dir, token, args, capped=capped)

    return scan_artist


def _candidate_specs(artist_dir: Path, found, hidden):
    filtered = []
    for candidate in found:
        spec = _candidate_spec(artist_dir.name, candidate)
        if hidden is not None and hidden_mod.is_hidden(
                hidden_mod.SCOPE_UPGRADE, artist_dir.name, spec["title"], hidden):
            continue
        filtered.append(spec)
    return filtered


def visible_candidates(state=None, hidden=None):
    state = load() if state is None else state
    if not state.get("complete"):
        return []
    hidden = hidden_mod.load() if hidden is None else hidden
    return [
        c for c in state.get("candidates") or []
        if not hidden_mod.is_hidden(
            hidden_mod.SCOPE_UPGRADE, c.get("artist"), c.get("title"), hidden)
    ]


def has_visible_candidates(state=None, hidden=None):
    return bool(visible_candidates(state=state, hidden=hidden))


def update_artist(
    artist_dir: Path,
    *,
    token,
    args,
    capped,
    hidden=None,
    scan_artist: Callable[[Path], list[dict]] | None = None,
):
    """Re-scan one artist and merge it into the saved upgrade snapshot."""
    if scan_artist is None:
        scan_artist = _default_scan_artist(token, args, capped)

    name = artist_dir.name
    fingerprint = artist_fingerprint(artist_dir)
    try:
        specs = _candidate_specs(artist_dir, scan_artist(artist_dir), hidden)
    except Exception as exc:
        return RefreshResult(
            [], [name], {name: str(exc)}, False, {name: fingerprint})

    with _STATE_LOCK:
        state = load()
        now = time.time()
        kept = [c for c in state["candidates"] if c.get("artist") != name]
        kept.extend(specs)
        artists_scanned = list(dict.fromkeys(
            list(state.get("artists_scanned") or []) + [name]))
        errors = dict(state.get("errors") or {})
        errors.pop(name, None)
        fingerprints = dict(state.get("fingerprints") or {})
        fingerprints[name] = fingerprint
        artist_updated_at = dict(state.get("artist_updated_at") or {})
        artist_updated_at[name] = now
        _write_state({
            "version": STATE_VERSION,
            "updated_at": now,
            "complete": bool(state.get("complete", True)),
            "artists_scanned": artists_scanned,
            "errors": errors,
            "fingerprints": fingerprints,
            "artist_updated_at": artist_updated_at,
            "hidden_signature": state.get("hidden_signature", ""),
            "candidates": kept,
        })
    return RefreshResult(specs, [name], {}, True, {name: fingerprint})


def refresh_for_artists(
    artists: Iterable[Path],
    *,
    token,
    args,
    capped,
    hidden=None,
    scan_artist: Callable[[Path], list[dict]] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    on_artist: Callable[[Path, list[dict], Exception | None, int, int], None] | None = None,
    workers: int = 1,
    pool_kwargs: dict | None = None,
    skip_unchanged: bool = False,
    persist: bool = True,
):
    """Refresh upgrade candidates for ``artists`` and persist review specs."""
    refresh_started_at = time.time()
    if scan_artist is None:
        scan_artist = _default_scan_artist(token, args, capped)

    artist_list = list(artists)
    specs: list[dict] = []
    artists_scanned: list[str] = []
    errors: dict[str, str] = {}
    complete = True
    total = len(artist_list)
    fingerprints: dict[str, str] = {}
    hidden_sig = _hidden_signature(hidden)
    previous = load()
    can_reuse = (
        skip_unchanged
        and previous.get("complete")
        and previous.get("hidden_signature", "") == hidden_sig
    )
    to_scan: list[Path] = []
    reused: list[tuple[Path, list[dict]]] = []

    for artist_dir in artist_list:
        fingerprint = artist_fingerprint(artist_dir)
        fingerprints[artist_dir.name] = fingerprint
        if can_reuse and (previous.get("fingerprints") or {}).get(artist_dir.name) == fingerprint:
            reused.append((
                artist_dir,
                [
                    c for c in previous.get("candidates", [])
                    if c.get("artist") == artist_dir.name
                ],
            ))
        else:
            to_scan.append(artist_dir)

    def _handle_result(artist_dir, found, error, done, prefiltered=False):
        nonlocal complete
        filtered = []
        if error is None:
            filtered = list(found) if prefiltered else _candidate_specs(
                artist_dir, found, hidden)
            specs.extend(filtered)
        else:
            errors[artist_dir.name] = str(error)
            complete = False
        artists_scanned.append(artist_dir.name)
        if on_artist is not None:
            on_artist(artist_dir, filtered, error, done, total)

    done_count = 0
    for artist_dir, existing in reused:
        done_count += 1
        _handle_result(artist_dir, existing, None, done_count, prefiltered=True)

    if workers <= 1:
        for artist_dir in to_scan:
            if cancel_check is not None and cancel_check():
                complete = False
                break
            done_count += 1
            try:
                _handle_result(artist_dir, scan_artist(artist_dir), None, done_count)
            except Exception as exc:
                _handle_result(artist_dir, [], exc, done_count)
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="upgradestate",
                                **(pool_kwargs or {})) as ex:
            futures = {ex.submit(scan_artist, artist_dir): artist_dir
                       for artist_dir in to_scan}
            for fut in as_completed(futures):
                if cancel_check is not None and cancel_check():
                    complete = False
                    for f in futures:
                        f.cancel()
                    break
                done_count += 1
                artist_dir = futures[fut]
                try:
                    _handle_result(artist_dir, fut.result(), None, done_count)
                except Exception as exc:
                    _handle_result(artist_dir, [], exc, done_count)

    result = RefreshResult(
        specs, artists_scanned, errors, complete, fingerprints, hidden_sig,
        refresh_started_at)
    # A cancelled refresh only contains the artists reached before the cancel.
    # Keep the last complete snapshot instead of turning a partial crawl into a
    # saved review list.
    if persist and result.complete:
        save(
            result,
            preserve_concurrent=True,
            refresh_started_at=refresh_started_at,
        )
    return result
