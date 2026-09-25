"""Saved scan candidates and per-artist revisions."""
import time
from dataclasses import dataclass, field

from qobuz_librarian import state_file
from qobuz_librarian.library import generation_state
from qobuz_librarian.ui_cli import logging as cli_logging


@dataclass
class RefreshResult:
    candidates: list
    artists_scanned: list[str]
    errors: dict[str, str]
    complete: bool
    fingerprints: dict[str, str] = field(default_factory=dict)
    refresh_started_at: float = 0.0


class ArtistScanStore:
    def __init__(
        self, *, path, lock, version, what, lost, surface, write_failure,
        candidate_to_dict, candidate_from_dict, quality_signature=None,
    ):
        self.path = path
        self.lock = lock
        self.version = version
        self.what = what
        self.lost = lost
        self.surface = surface
        self.write_failure = write_failure
        self.candidate_to_dict = candidate_to_dict
        self.candidate_from_dict = candidate_from_dict
        self.quality_signature = quality_signature

    def empty_state(self):
        data = {
            "version": self.version,
            "updated_at": None,
            "generation": 0,
            "revision": 0,
            "complete": False,
            "artists_scanned": [],
            "errors": {},
            "fingerprints": {},
            "artist_updated_at": {},
            "artist_revision": {},
        }
        if self.quality_signature is not None:
            data["quality_signature"] = ""
        data["candidates"] = []
        return data

    def load(self):
        data = state_file.load_json_object(self.path(), self.what, self.lost)
        # A version the build doesn't know is a deliberate schema signal, not
        # corruption: leave the file alone and rebuild from a fresh scan.
        if data is None or data.get("version") != self.version:
            return self.empty_state()
        base = self.empty_state()
        updated_at, updated_ok = state_file.optional_time(data.get("updated_at"))
        generation, generation_ok = state_file.nonnegative_int(data.get("generation"))
        revision, revision_ok = state_file.nonnegative_int(data.get("revision"))
        raw_artists = data.get("artists_scanned")
        artists_ok = raw_artists is None or isinstance(raw_artists, list)
        artists_scanned = (
            [str(name) for name in raw_artists if isinstance(name, str)]
            if isinstance(raw_artists, list) else []
        )
        if isinstance(raw_artists, list) and len(artists_scanned) != len(raw_artists):
            artists_ok = False
        raw_candidates = data.get("candidates")
        candidates_ok = raw_candidates is None or isinstance(raw_candidates, list)
        candidates = [
            candidate for candidate in (raw_candidates or [])
            if isinstance(candidate, dict)
            and (
                candidate.get("payload") is None
                or isinstance(candidate.get("payload"), dict)
            )
        ] if candidates_ok else []
        if isinstance(raw_candidates, list) and len(candidates) != len(raw_candidates):
            candidates_ok = False
        base.update({
            "updated_at": updated_at,
            "generation": generation,
            "revision": revision,
            "complete": bool(data.get("complete")) and all((
                updated_ok, generation_ok, revision_ok, artists_ok, candidates_ok,
            )),
            "artists_scanned": artists_scanned,
            "errors": data.get("errors") if isinstance(data.get("errors"), dict) else {},
            "fingerprints": (data.get("fingerprints")
                             if isinstance(data.get("fingerprints"), dict) else {}),
            "artist_updated_at": (data.get("artist_updated_at")
                                  if isinstance(data.get("artist_updated_at"), dict)
                                  else {}),
            "artist_revision": (data.get("artist_revision")
                                if isinstance(data.get("artist_revision"), dict)
                                else {}),
            "candidates": candidates,
        })
        if self.quality_signature is not None:
            base["quality_signature"] = str(data.get("quality_signature") or "")
        return base

    def write_state(self, data):
        try:
            state_file.write_json(self.path(), data)
            return True
        except OSError as e:
            cli_logging.vlog(self.write_failure.format(e=e))
            return False

    def state_from_result(self, result, *, generation, revision):
        now = time.time()
        data = {
            "version": self.version,
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
        }
        if self.quality_signature is not None:
            data["quality_signature"] = (
                getattr(result, "quality_signature", "") or self.quality_signature()
            )
        data["candidates"] = [self.candidate_to_dict(c) for c in result.candidates]
        return data

    def preserve_concurrent_artist_updates(
        self, data, refresh_started_at, refresh_started_revision, *, load,
    ):
        if not refresh_started_at and not refresh_started_revision:
            return data
        current = load()
        if (
            self.quality_signature is not None
            and current.get("quality_signature") != data.get("quality_signature")
        ):
            return data
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
        self, result, *, load, write_state, preserve_concurrent=False,
        refresh_started_at=None, refresh_started_revision=None,
        generation=None, revision=None,
    ):
        with self.lock, state_file.store_lock(self.path()):
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
            data = self.state_from_result(
                result,
                generation=target_generation,
                revision=target_revision,
            )
            if preserve_concurrent:
                data = self.preserve_concurrent_artist_updates(
                    data,
                    refresh_started_at
                    if refresh_started_at is not None
                    else result.refresh_started_at,
                    refresh_started_revision
                    if refresh_started_revision is not None
                    else result.refresh_started_revision,
                    load=load,
                )
            if not write_state(data):
                return False
            policy = {}
            if self.quality_signature is not None:
                policy["policy_signature"] = data.get("quality_signature", "")
            return generation_state.mark_output_current(
                self.surface,
                generation=target_generation,
                revision=target_revision,
                complete=result.complete,
                **policy,
            )

    def state_with_artist(
        self, state, name, fingerprint, candidates, *, now, generation, revision,
        quality_signature="",
    ):
        kept = [c for c in state["candidates"] if c.get("artist") != name]
        kept.extend(self.candidate_to_dict(c) for c in candidates)
        artists_scanned = list(dict.fromkeys(
            list(state.get("artists_scanned") or []) + [name]))
        errors = dict(state.get("errors") or {})
        errors.pop(name, None)
        fingerprints = dict(state.get("fingerprints") or {})
        fingerprints[name] = fingerprint
        artist_updated_at = dict(state.get("artist_updated_at") or {})
        artist_updated_at[name] = now
        artist_revision = dict(state.get("artist_revision") or {})
        artist_revision[name] = revision
        data = {
            "version": self.version,
            "updated_at": now,
            "generation": generation,
            "revision": revision,
            "complete": bool(state.get("complete", True)),
            "artists_scanned": artists_scanned,
            "errors": errors,
            "fingerprints": fingerprints,
            "artist_updated_at": artist_updated_at,
            "artist_revision": artist_revision,
        }
        if self.quality_signature is not None:
            data["quality_signature"] = quality_signature
        data["candidates"] = kept
        return data

    def candidates_for_artist(self, state, name):
        return [
            self.candidate_from_dict(c) for c in state.get("candidates", [])
            if c.get("artist") == name
        ]


def finish_refresh(result, *, persist, save):
    # A cancelled refresh only contains the artists reached before the cancel.
    # Keep the last complete snapshot instead of turning a partial crawl into a
    # saved review list.
    if persist and result.complete:
        save(
            result,
            preserve_concurrent=True,
            refresh_started_at=result.refresh_started_at,
            refresh_started_revision=result.refresh_started_revision,
        )
    return result
