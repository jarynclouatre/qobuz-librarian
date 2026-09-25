"""Shared downsample scan state."""
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file
from qobuz_librarian.library import (
    artist_scan_store,
    candidate_premise,
    downsample,
    generation_state,
)
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library.artist_fingerprint import artist_fingerprint
from qobuz_librarian.library.downsample import DownsampleCandidate

STATE_VERSION = 1
_STATE_LOCK = threading.Lock()


@dataclass
class RefreshResult(artist_scan_store.RefreshResult):
    candidates: list[DownsampleCandidate]
    refresh_started_revision: int = 0


def _candidate_to_dict(c: DownsampleCandidate):
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
    premise = candidate_premise.capture("downsample", c.album_dir)
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


_store = artist_scan_store.ArtistScanStore(
    path=lambda: cfg.DOWNSAMPLE_STATE_FILE,
    lock=_STATE_LOCK,
    version=STATE_VERSION,
    what="the saved downsample scan",
    lost="the Downsample results from your last Library refresh",
    surface="downsample",
    write_failure="downsample state write failed ({e}); saved downsample view may be stale",
    candidate_to_dict=_candidate_to_dict,
    candidate_from_dict=_candidate_from_dict,
)


def load():
    return _store.load()


def _write_state(data):
    return _store.write_state(data)


def save(
    result: RefreshResult,
    *,
    preserve_concurrent: bool = False,
    refresh_started_at=None,
    refresh_started_revision=None,
    generation=None,
    revision=None,
):
    return _store.save(
        result,
        load=load,
        write_state=_write_state,
        preserve_concurrent=preserve_concurrent,
        refresh_started_at=refresh_started_at,
        refresh_started_revision=refresh_started_revision,
        generation=generation,
        revision=revision,
    )


def visible_of(candidates, hidden=None):
    """The candidates a user should see. The snapshot keeps kept-hi-res albums
    so bringing one back has a record to restore from, so every path that shows
    scan results filters here."""
    hidden = hidden_mod.load() if hidden is None else hidden
    return [
        cand for cand in candidates
        if not hidden_mod.is_hidden(
            hidden_mod.SCOPE_DOWNSAMPLE, cand.artist, cand.title, hidden)
    ]


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
    scan_artist: Callable[[Path], list[DownsampleCandidate]] | None = None,
):
    """Re-scan one artist and merge it into the saved downsample snapshot."""
    if scan_artist is None:
        scan_artist = downsample.scan_artist_for_downsample

    name = artist_dir.name
    fingerprints: dict[str, str] = {}
    try:
        fingerprint = artist_fingerprint(artist_dir)
        fingerprints[name] = fingerprint
        filtered = list(scan_artist(artist_dir))
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
        saved = _write_state(_store.state_with_artist(
            state, name, fingerprint, filtered,
            now=now, generation=target_generation, revision=state_revision,
        ))
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
    discovery_errors: dict[str, str] | None = None,
):
    """Refresh downsample candidates for ``artists`` and persist the result."""
    refresh_started_at = time.time()
    refresh_started_revision = generation_state.revision()
    if scan_artist is None:
        scan_artist = downsample.scan_artist_for_downsample

    artist_list = list(artists)
    candidates: list[DownsampleCandidate] = []
    artists_scanned: list[str] = []
    errors: dict[str, str] = dict(discovery_errors or {})
    complete = not errors
    total = len(artist_list)
    fingerprints: dict[str, str] = {}
    previous = load() if skip_unchanged else {}
    # Keeping or bringing back a hi-res album is a filter over the saved
    # candidates, not a reason to re-read every artist's files.
    can_reuse = skip_unchanged and previous.get("complete")

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
                found = _store.candidates_for_artist(previous, artist_dir.name)
                candidates.extend(found)
                filtered = visible_of(found, hidden)
                artists_scanned.append(artist_dir.name)
                if on_artist is not None:
                    on_artist(artist_dir, filtered, error, idx, total)
                continue
            found = list(scan_artist(artist_dir))
            candidates.extend(found)
            filtered = visible_of(found, hidden)
            artists_scanned.append(artist_dir.name)
        except Exception as exc:
            error = exc
            errors[artist_dir.name] = str(exc)
            complete = False
            artists_scanned.append(artist_dir.name)
        if on_artist is not None:
            on_artist(artist_dir, filtered, error, idx, total)

    result = RefreshResult(
        candidates, artists_scanned, errors, complete, fingerprints,
        refresh_started_at, refresh_started_revision)
    return artist_scan_store.finish_refresh(result, persist=persist, save=save)
