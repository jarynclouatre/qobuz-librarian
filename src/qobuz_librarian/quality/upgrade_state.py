"""Shared upgrade scan state."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file
from qobuz_librarian.library import artist_scan_store, candidate_premise, generation_state
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library.artist_fingerprint import artist_fingerprint
from qobuz_librarian.library.catalog import album_year
from qobuz_librarian.quality import decision as quality_decision

STATE_VERSION = 1
_STATE_LOCK = threading.Lock()


@dataclass
class RefreshResult(artist_scan_store.RefreshResult):
    candidates: list[dict]
    quality_signature: str = ""
    refresh_started_revision: int = 0


def quality_signature(streamrip_quality=None, prefer_hires=None) -> str:
    """Quality policy that shapes Upgrade candidates and their labels."""
    if streamrip_quality is None:
        streamrip_quality = getattr(cfg, "STREAMRIP_QUALITY", "")
    if prefer_hires is None:
        prefer_hires = getattr(cfg, "PREFER_HIRES", False)
    return f"{streamrip_quality}|{bool(prefer_hires)}"


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
    album_dir = candidate.get("album_dir")
    payload = {
        "album_id": album.get("id"),
        # Where the scan found the album locally, so a later walk can tell
        # a candidate that's still there from one whose folder has gone.
        "album_dir": str(album_dir or ""),
        "year": album_year(album),
        "cover": _album_cover(album),
        "needed_edition_swap": bool(candidate.get("_needed_edition_swap")),
        "title_similarity": float(candidate.get("_title_similarity") or 0.0),
    }
    premise = candidate_premise.capture("upgrade", album_dir) if album_dir else None
    if premise is not None:
        payload["_premise"] = premise
    return {
        "title": title,
        "artist": artist_name,
        "detail": (f"{candidate.get('existing_quality_label', '?')} → "
                   f"{candidate.get('target_quality_label', '?')}{part}"),
        "payload": payload,
    }


_store = artist_scan_store.ArtistScanStore(
    path=lambda: cfg.UPGRADE_STATE_FILE,
    lock=_STATE_LOCK,
    version=STATE_VERSION,
    what="the saved upgrade scan",
    lost="the Upgrade results from your last Library refresh",
    surface="upgrade",
    write_failure="upgrade state write failed ({e}); saved upgrade view may be stale",
    candidate_to_dict=lambda c: c,
    candidate_from_dict=lambda c: c,
    quality_signature=lambda: quality_signature(),
)


def load():
    return _store.load()


def _write_state(data):
    return _store.write_state(data)


def reusable_result(result: RefreshResult) -> dict:
    """A finished refresh shaped as the saved state a refresh reuses."""
    return {
        "complete": bool(result.complete),
        "quality_signature": (
            getattr(result, "quality_signature", "") or quality_signature()
        ),
        "fingerprints": dict(getattr(result, "fingerprints", None) or {}),
        "candidates": list(result.candidates),
    }


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


def _default_scan_artist(token, args, capped):
    def scan_artist(artist_dir):
        return quality_decision.scan_artist_for_upgrades(
            artist_dir.name, artist_dir, token, args, capped=capped)

    return scan_artist


def _candidate_specs(artist_dir: Path, found):
    return [_candidate_spec(artist_dir.name, candidate) for candidate in found]


def visible_of(specs, hidden=None):
    """The candidates a user should see. The snapshot keeps dismissed upgrades
    so bringing one back has a record to restore from, so every path that shows
    scan results filters here."""
    hidden = hidden_mod.load() if hidden is None else hidden
    return [
        spec for spec in specs
        if not hidden_mod.is_hidden(
            hidden_mod.SCOPE_UPGRADE,
            spec.get("artist"),
            spec.get("title"),
            hidden,
            year=(spec.get("payload") or {}).get("year"),
        )
    ]


def visible_candidates(state=None, hidden=None):
    state = load() if state is None else state
    if not state.get("complete"):
        return []
    return visible_of(state.get("candidates") or [], hidden)


def has_visible_candidates(state=None, hidden=None):
    return bool(visible_candidates(state=state, hidden=hidden))


def remove_album_dir(album_dir) -> bool:
    """Suppress one exact locally-capped album without contacting Qobuz."""
    def resolved(value):
        try:
            return str(Path(value).resolve(strict=False))
        except (OSError, TypeError, ValueError):
            return None

    target = resolved(album_dir)
    if target is None:
        return False
    with _STATE_LOCK, state_file.store_lock(cfg.UPGRADE_STATE_FILE):
        state = load()
        generation = int(state.get("generation") or 0)
        if generation != generation_state.current_generation():
            return False
        kept = [
            candidate
            for candidate in state.get("candidates") or []
            if resolved((candidate.get("payload") or {}).get("album_dir"))
            != target
        ]
        if len(kept) == len(state.get("candidates") or []):
            return True
        revision = generation_state.reserve_revision()
        if revision is None:
            return False
        now = time.time()
        state = {
            **state,
            "updated_at": now,
            "revision": revision,
            "candidates": kept,
        }
        if not _write_state(state):
            return False
        return generation_state.mark_output_current(
            "upgrade",
            generation=generation,
            revision=revision,
            complete=bool(state.get("complete")),
            policy_signature=str(state.get("quality_signature") or ""),
            preserve_noncurrent=True,
        )


def update_artist(
    artist_dir: Path,
    *,
    token,
    args,
    capped,
    scan_artist: Callable[[Path], list[dict]] | None = None,
):
    """Re-scan one artist and merge it into the saved upgrade snapshot."""
    if scan_artist is None:
        scan_artist = _default_scan_artist(token, args, capped)

    name = artist_dir.name
    scan_quality_signature = quality_signature()
    fingerprints: dict[str, str] = {}
    try:
        fingerprint = artist_fingerprint(artist_dir)
        fingerprints[name] = fingerprint
        specs = _candidate_specs(artist_dir, scan_artist(artist_dir))
    except Exception as exc:
        return RefreshResult([], [name], {name: str(exc)}, False, fingerprints)

    with _STATE_LOCK, state_file.store_lock(cfg.UPGRADE_STATE_FILE):
        state = load()
        now = time.time()
        target_generation = generation_state.current_generation()
        state_revision = generation_state.reserve_revision()
        if state_revision is None:
            return RefreshResult(
                [], [name], {name: "saved state revision could not be written"},
                False, {name: fingerprint},
                quality_signature=scan_quality_signature,
            )
        saved = _write_state(_store.state_with_artist(
            state, name, fingerprint, specs,
            now=now, generation=target_generation, revision=state_revision,
            quality_signature=(
                scan_quality_signature
                if state.get("quality_signature") == scan_quality_signature
                else state.get("quality_signature", "")
            ),
        ))
        authority_saved = saved and generation_state.mark_output_current(
            "upgrade",
            generation=target_generation,
            revision=state_revision,
            complete=bool(state.get("complete", True)),
            policy_signature=scan_quality_signature,
            preserve_noncurrent=True,
        )
    if not authority_saved:
        return RefreshResult(
            [], [name], {name: "saved Upgrade view needs refresh"}, False,
            {name: fingerprint}, quality_signature=scan_quality_signature,
        )
    return RefreshResult(
        specs,
        [name],
        {},
        True,
        {name: fingerprint},
        quality_signature=scan_quality_signature,
    )


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
    discovery_errors: dict[str, str] | None = None,
    previous: dict | None = None,
):
    """Refresh upgrade candidates for ``artists`` and persist review specs."""
    refresh_started_at = time.time()
    refresh_started_revision = generation_state.revision()
    scan_quality_signature = quality_signature()
    if scan_artist is None:
        scan_artist = _default_scan_artist(token, args, capped)

    artist_list = list(artists)
    specs: list[dict] = []
    artists_scanned: list[str] = []
    errors: dict[str, str] = dict(discovery_errors or {})
    complete = not errors
    total = len(artist_list)
    fingerprints: dict[str, str] = {}
    if previous is None:
        previous = load() if skip_unchanged else {}
    # Dismissing or bringing back an upgrade is a filter over the saved
    # candidates, not a reason to ask Qobuz about every artist again.
    can_reuse = (
        skip_unchanged
        and previous.get("complete")
        and previous.get("quality_signature", "") == scan_quality_signature
    )
    to_scan: list[Path] = []
    reused: list[tuple[Path, list[dict]]] = []
    fingerprint_failures: list[tuple[Path, Exception]] = []

    for artist_dir in artist_list:
        try:
            fingerprint = artist_fingerprint(artist_dir)
        except Exception as exc:
            fingerprint_failures.append((artist_dir, exc))
            continue
        fingerprints[artist_dir.name] = fingerprint
        if can_reuse and (previous.get("fingerprints") or {}).get(artist_dir.name) == fingerprint:
            reused.append((
                artist_dir,
                _store.candidates_for_artist(previous, artist_dir.name),
            ))
        else:
            to_scan.append(artist_dir)

    def _handle_result(artist_dir, found, error, done, saved=False):
        nonlocal complete
        visible = []
        if error is None:
            scanned = list(found) if saved else _candidate_specs(artist_dir, found)
            specs.extend(scanned)
            visible = visible_of(scanned, hidden)
        else:
            errors[artist_dir.name] = str(error)
            complete = False
        artists_scanned.append(artist_dir.name)
        if on_artist is not None:
            on_artist(artist_dir, visible, error, done, total)

    done_count = 0
    for artist_dir, error in fingerprint_failures:
        done_count += 1
        _handle_result(artist_dir, [], error, done_count)
    for artist_dir, existing in reused:
        done_count += 1
        _handle_result(artist_dir, existing, None, done_count, saved=True)

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
        specs, artists_scanned, errors, complete, fingerprints,
        refresh_started_at, scan_quality_signature, refresh_started_revision)
    return artist_scan_store.finish_refresh(result, persist=persist, save=save)
