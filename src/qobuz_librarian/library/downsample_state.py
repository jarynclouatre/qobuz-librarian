"""Shared downsample scan state.

The downsample pass is local-only, so both the standalone Downsample scan and
the baseline Library scan can refresh the same candidate state without mixing
downsample items into the Library review list.
"""
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file
from qobuz_librarian.library import generation_state
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library.artist_fingerprint import artist_fingerprint
from qobuz_librarian.library.downsample import DownsampleCandidate

STATE_VERSION = 1
_STATE_LOCK = threading.Lock()


@dataclass
class RefreshResult:
    candidates: list[DownsampleCandidate]
    artists_scanned: list[str]
    errors: dict[str, str]
    complete: bool
    fingerprints: dict[str, str] = field(default_factory=dict)
    hidden_signature: str = ""
    refresh_started_at: float = 0.0
    refresh_started_revision: int = 0


def _empty_state():
    return {
        "version": STATE_VERSION,
        "updated_at": None,
        "generation": 0,
        "revision": 0,
        "complete": False,
        "artists_scanned": [],
        "errors": {},
        "fingerprints": {},
        "artist_updated_at": {},
        "artist_revision": {},
        "hidden_signature": "",
        "candidates": [],
    }


def _candidate_to_dict(c: DownsampleCandidate):
    from qobuz_librarian.library.candidate_premise import capture

    value = {
        "album_dir": str(c.album_dir),
        "artist": c.artist,
        "title": c.title,
        "n_hires": c.n_hires,
        "n_flac": c.n_flac,
        "source_rates": list(c.source_rates),
        "target_rates": list(c.target_rates),
        "est_saving": c.est_saving,
        "detail": c.detail,
    }
    premise = capture("downsample", c.album_dir)
    if premise is not None:
        value["_premise"] = premise
    return value


def _candidate_from_dict(data):
    return DownsampleCandidate(
        album_dir=Path(data.get("album_dir") or ""),
        artist=data.get("artist") or "",
        title=data.get("title") or "",
        n_hires=int(data.get("n_hires") or 0),
        n_flac=int(data.get("n_flac") or 0),
        source_rates=list(data.get("source_rates") or []),
        target_rates=list(data.get("target_rates") or []),
        est_saving=int(data.get("est_saving") or 0),
    )


def _hidden_signature(hidden):
    if hidden is None:
        return ""
    bucket = hidden.get(hidden_mod.SCOPE_DOWNSAMPLE) if isinstance(hidden, dict) else {}
    if not isinstance(bucket, dict):
        bucket = {}
    return json.dumps(bucket, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"))


def load():
    data = state_file.load_json_object(
        cfg.DOWNSAMPLE_STATE_FILE, "the saved downsample scan",
        "the Downsample candidates from your last Library refresh")
    # A version the build doesn't know is a deliberate schema signal, not
    # corruption: leave the file alone and rebuild from a fresh scan.
    if data is None or data.get("version") != STATE_VERSION:
        return _empty_state()
    base = _empty_state()
    base.update({
        "updated_at": data.get("updated_at"),
        "generation": int(data.get("generation") or 0),
        "revision": int(data.get("revision") or 0),
        "complete": bool(data.get("complete")),
        "artists_scanned": list(data.get("artists_scanned") or []),
        "errors": data.get("errors") if isinstance(data.get("errors"), dict) else {},
        "fingerprints": (data.get("fingerprints")
                         if isinstance(data.get("fingerprints"), dict) else {}),
        "artist_updated_at": (data.get("artist_updated_at")
                              if isinstance(data.get("artist_updated_at"), dict)
                              else {}),
        "artist_revision": (data.get("artist_revision")
                            if isinstance(data.get("artist_revision"), dict)
                            else {}),
        "hidden_signature": str(data.get("hidden_signature") or ""),
        "candidates": (data.get("candidates")
                       if isinstance(data.get("candidates"), list) else []),
    })
    return base


def _write_state(data):
    try:
        state_file.write_json(cfg.DOWNSAMPLE_STATE_FILE, data)
        return True
    except OSError as e:
        # The Downsample view reads this snapshot; a failed write means it
        # shows stale candidates until the next scan.
        from qobuz_librarian.ui_cli.logging import vlog
        vlog(f"downsample state write failed ({e}); saved downsample view may be stale")
        return False


def _state_from_result(result: RefreshResult, *, generation: int, revision: int):
    now = time.time()
    return {
        "version": STATE_VERSION,
        "updated_at": now,
        "generation": int(generation),
        "revision": int(revision),
        "complete": bool(result.complete),
        "artists_scanned": list(result.artists_scanned),
        "errors": dict(result.errors),
        "fingerprints": dict(result.fingerprints),
        "artist_updated_at": {name: now for name in result.artists_scanned},
        "artist_revision": {
            name: int(revision) for name in result.artists_scanned
        },
        "hidden_signature": result.hidden_signature,
        "candidates": [_candidate_to_dict(c) for c in result.candidates],
    }


def _preserve_concurrent_artist_updates(
    data,
    refresh_started_at,
    refresh_started_revision,
):
    if not refresh_started_at and not refresh_started_revision:
        return data
    current = load()
    current_artist_updated_at = current.get("artist_updated_at") or {}
    current_artist_revision = current.get("artist_revision") or {}
    current_fingerprints = current.get("fingerprints") or {}
    if refresh_started_revision:
        preserved_artists = {
            name for name, artist_revision in current_artist_revision.items()
            if int(artist_revision or 0) > int(refresh_started_revision)
        }
    else:
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
    data_artist_revision = dict(data.get("artist_revision") or {})
    for name in preserved_artists:
        data["fingerprints"][name] = current_fingerprints.get(name, "")
        data_artist_updated_at[name] = current_artist_updated_at.get(name, 0)
        data_artist_revision[name] = current_artist_revision.get(name, 0)
    data["artist_updated_at"] = data_artist_updated_at
    data["artist_revision"] = data_artist_revision
    return data


def save(
    result: RefreshResult,
    *,
    preserve_concurrent: bool = False,
    refresh_started_at=None,
    refresh_started_revision=None,
    generation=None,
    revision=None,
):
    with _STATE_LOCK, state_file.store_lock(cfg.DOWNSAMPLE_STATE_FILE):
        target_generation = (
            generation_state.current_generation()
            if generation is None
            else int(generation)
        )
        target_revision = (
            generation_state.reserve_revision()
            if revision is None
            else int(revision)
        )
        if target_revision is None:
            return False
        data = _state_from_result(
            result,
            generation=target_generation,
            revision=target_revision,
        )
        if preserve_concurrent:
            data = _preserve_concurrent_artist_updates(
                data,
                refresh_started_at
                if refresh_started_at is not None
                else result.refresh_started_at,
                refresh_started_revision
                if refresh_started_revision is not None
                else result.refresh_started_revision,
            )
        if not _write_state(data):
            return False
        return generation_state.mark_output_current(
            "downsample",
            generation=target_generation,
            revision=target_revision,
            complete=result.complete,
        )


def _scan_artist(artist_dir: Path, scan_artist, hidden):
    found = scan_artist(artist_dir)
    filtered: list[DownsampleCandidate] = []
    for cand in found:
        if hidden is not None and hidden_mod.is_hidden(
                hidden_mod.SCOPE_DOWNSAMPLE, cand.artist, cand.title, hidden):
            continue
        filtered.append(cand)
    return filtered


def visible_candidates(state=None, hidden=None):
    state = load() if state is None else state
    if not state.get("complete"):
        return []
    hidden = hidden_mod.load() if hidden is None else hidden
    return [
        c for c in state.get("candidates") or []
        if not hidden_mod.is_hidden(
            hidden_mod.SCOPE_DOWNSAMPLE, c.get("artist"), c.get("title"), hidden)
    ]


def has_visible_candidates(state=None, hidden=None):
    return bool(visible_candidates(state=state, hidden=hidden))


def update_artist(
    artist_dir: Path,
    *,
    hidden=None,
    scan_artist: Callable[[Path], list[DownsampleCandidate]] | None = None,
):
    """Re-scan one artist and merge it into the saved downsample snapshot."""
    if scan_artist is None:
        from qobuz_librarian.library.downsample import scan_artist_for_downsample
        scan_artist = scan_artist_for_downsample

    name = artist_dir.name
    fingerprints: dict[str, str] = {}
    try:
        fingerprint = artist_fingerprint(artist_dir)
        fingerprints[name] = fingerprint
        filtered = _scan_artist(artist_dir, scan_artist, hidden)
    except Exception as exc:
        return RefreshResult([], [name], {name: str(exc)}, False, fingerprints)

    with _STATE_LOCK, state_file.store_lock(cfg.DOWNSAMPLE_STATE_FILE):
        state = load()
        now = time.time()
        target_generation = generation_state.current_generation()
        state_revision = generation_state.reserve_revision()
        if state_revision is None:
            return RefreshResult(
                [], [name], {name: "saved state revision could not be written"},
                False, {name: fingerprint},
            )
        kept = [c for c in state["candidates"] if c.get("artist") != name]
        kept.extend(_candidate_to_dict(c) for c in filtered)
        artists_scanned = list(dict.fromkeys(
            list(state.get("artists_scanned") or []) + [name]))
        errors = dict(state.get("errors") or {})
        errors.pop(name, None)
        fingerprints = dict(state.get("fingerprints") or {})
        fingerprints[name] = fingerprint
        artist_updated_at = dict(state.get("artist_updated_at") or {})
        artist_updated_at[name] = now
        artist_revision = dict(state.get("artist_revision") or {})
        artist_revision[name] = state_revision
        saved = _write_state({
            "version": STATE_VERSION,
            "updated_at": now,
            "generation": target_generation,
            "revision": state_revision,
            "complete": bool(state.get("complete", True)),
            "artists_scanned": artists_scanned,
            "errors": errors,
            "fingerprints": fingerprints,
            "artist_updated_at": artist_updated_at,
            "artist_revision": artist_revision,
            "hidden_signature": state.get("hidden_signature", ""),
            "candidates": kept,
        })
        authority_saved = saved and generation_state.mark_output_current(
            "downsample",
            generation=target_generation,
            revision=state_revision,
            complete=bool(state.get("complete", True)),
            preserve_noncurrent=True,
        )
    if not authority_saved:
        return RefreshResult(
            [], [name], {name: "saved Downsample view needs refresh"}, False,
            {name: fingerprint},
        )
    return RefreshResult(filtered, [name], {}, True, {name: fingerprint})


def remove_artist(name: str):
    """Remove one artist from the saved downsample snapshot."""
    with _STATE_LOCK, state_file.store_lock(cfg.DOWNSAMPLE_STATE_FILE):
        state = load()
        now = time.time()
        target_generation = generation_state.current_generation()
        state_revision = generation_state.reserve_revision()
        if state_revision is None:
            return RefreshResult(
                [], [name], {name: "saved state revision could not be written"},
                False, {},
            )
        artists_scanned = [
            artist for artist in state.get("artists_scanned") or []
            if artist != name
        ]
        errors = dict(state.get("errors") or {})
        errors.pop(name, None)
        fingerprints = dict(state.get("fingerprints") or {})
        fingerprints.pop(name, None)
        artist_updated_at = dict(state.get("artist_updated_at") or {})
        artist_updated_at.pop(name, None)
        artist_revision = dict(state.get("artist_revision") or {})
        artist_revision.pop(name, None)
        saved = _write_state({
            "version": STATE_VERSION,
            "updated_at": now,
            "generation": target_generation,
            "revision": state_revision,
            "complete": bool(state.get("complete", True)),
            "artists_scanned": artists_scanned,
            "errors": errors,
            "fingerprints": fingerprints,
            "artist_updated_at": artist_updated_at,
            "artist_revision": artist_revision,
            "hidden_signature": state.get("hidden_signature", ""),
            "candidates": [
                c for c in state.get("candidates") or []
                if c.get("artist") != name
            ],
        })
        authority_saved = saved and generation_state.mark_output_current(
            "downsample",
            generation=target_generation,
            revision=state_revision,
            complete=bool(state.get("complete", True)),
            preserve_noncurrent=True,
        )
    if not authority_saved:
        return RefreshResult(
            [], [name], {name: "saved Downsample view needs refresh"}, False, {}
        )
    return RefreshResult([], [name], {}, True, {})


def refresh_for_artists(
    artists: Iterable[Path],
    *,
    hidden=None,
    scan_artist: Callable[[Path], list[DownsampleCandidate]] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    on_artist: Callable[[Path, list[DownsampleCandidate], Exception | None, int, int], None] | None = None,
    persist: bool = True,
    skip_unchanged: bool = False,
):
    """Refresh downsample candidates for ``artists`` and persist the result."""
    refresh_started_at = time.time()
    refresh_started_revision = generation_state.revision()
    if scan_artist is None:
        from qobuz_librarian.library.downsample import scan_artist_for_downsample
        scan_artist = scan_artist_for_downsample

    artist_list = list(artists)
    candidates: list[DownsampleCandidate] = []
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

    for idx, artist_dir in enumerate(artist_list, 1):
        if cancel_check is not None and cancel_check():
            complete = False
            break
        error = None
        filtered: list[DownsampleCandidate] = []
        try:
            fingerprint = artist_fingerprint(artist_dir)
            fingerprints[artist_dir.name] = fingerprint
            if can_reuse and (previous.get("fingerprints") or {}).get(artist_dir.name) == fingerprint:
                filtered = [
                    _candidate_from_dict(c)
                    for c in previous.get("candidates", [])
                    if c.get("artist") == artist_dir.name
                ]
                candidates.extend(filtered)
                artists_scanned.append(artist_dir.name)
                if on_artist is not None:
                    on_artist(artist_dir, filtered, error, idx, total)
                continue
            filtered = _scan_artist(artist_dir, scan_artist, hidden)
            candidates.extend(filtered)
            artists_scanned.append(artist_dir.name)
        except Exception as exc:
            error = exc
            errors[artist_dir.name] = str(exc)
            complete = False
            artists_scanned.append(artist_dir.name)
        if on_artist is not None:
            on_artist(artist_dir, filtered, error, idx, total)

    result = RefreshResult(
        candidates, artists_scanned, errors, complete, fingerprints, hidden_sig,
        refresh_started_at, refresh_started_revision)
    # A cancelled refresh only contains the artists reached before the cancel.
    if persist and result.complete:
        save(
            result,
            preserve_concurrent=True,
            refresh_started_at=refresh_started_at,
            refresh_started_revision=refresh_started_revision,
        )
    return result
