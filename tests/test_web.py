"""Tests for the web UI: background job system (jobs.py) and HTTP routes (app.py).

Trimmed to a maintainable representative set: data-safety paths (restore,
hide/restore round-trip, migration move-vs-copy, persist-without-tearing),
auth/session/CSRF, the run-lock destructive-route guard, settings save/load,
one search + one approve endpoint, and a few genuinely tricky bits of logic.
"""

import asyncio
import copy
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from qobuz_librarian.web import jobs as jm

# ── jobs.py: worker loop ──────────────────────────────────────────────────────


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _allow_legacy_candidate_execution(monkeypatch):
    """Let older fixtures reach the executor behavior they exercise."""
    from qobuz_librarian.library import candidate_premise

    def allow(candidate, **_kwargs):
        return {
            "kind": candidate_premise.expected_kind(candidate),
            "receipt": None,
        }

    monkeypatch.setattr(candidate_premise, "validate", allow)
    monkeypatch.setattr(candidate_premise, "validate_container", allow)


def _make_saved_surface_current(monkeypatch, surface, state):
    """Give a mocked saved snapshot a current Library revision."""
    from qobuz_librarian.library import generation_state

    state["generation"] = 1
    state["revision"] = 2
    monkeypatch.setattr(
        generation_state,
        "load",
        lambda: {
            "generation": 1,
            "revision": 3,
            "catalog_complete": True,
            "outputs": {
                surface: {
                    "generation": 1,
                    "revision": 2,
                    "status": "current",
                    "complete": True,
                },
            },
        },
    )


def _repair_recovery_record(location, receipt=None):
    return {
        "version": 1,
        "kind": "repair-backup",
        "status": "retained",
        "location": str(location),
        "album_dir": "/music/Artist/Album",
        "stage": "refill",
        "reason": "Repair stopped while downloading the replacement.",
        "complete": True,
        "requested": 1,
        "backed_up": 1,
        "receipt": receipt,
    }


def test_a_busy_lane_serialises_album_work_and_drops_a_cancelled_queued_scan():
    """Both lanes interleave at the album level: only one rip+import at a
    time, even with two workers running, which guards against /staging races
    and beets' SQLite write lock. A scan queued behind a busy lane and
    cancelled before it starts is finalized to CANCELED at once instead of
    lingering as "Queued", and the worker drops it when the lane frees."""
    import threading

    jm.start_worker()
    inside = threading.Event()
    release = threading.Event()
    second_inside = threading.Event()

    holder = jm.Job(title="lock holder")
    holder.kind = "scan"

    def _hold(j):
        with jm.staging_lock():
            inside.set()
            release.wait(timeout=5)

    jm.registry.add(holder)
    jm._scan_queue.put((holder, _hold))
    assert inside.wait(timeout=5)

    contender = jm.Job(title="lock contender")
    contender_started = threading.Event()

    def _grab(j):
        contender_started.set()   # worker picked up the job; now it blocks on the lock
        with jm.staging_lock():
            second_inside.set()

    jm.submit(contender, _grab)
    # Wait until the worker has actually entered _grab (it is now blocking on
    # staging_lock, which the holder still owns).
    assert contender_started.wait(timeout=5), "download-lane worker never picked up contender"
    assert not second_inside.wait(timeout=0.3)
    release.set()
    assert second_inside.wait(timeout=5)

    release = threading.Event()
    holding = threading.Event()
    holder = jm.Job(title="lane holder")
    holder.kind = "scan"

    def _hold_lane(j):
        holding.set()
        release.wait(timeout=5)

    jm.registry.add(holder)
    jm._scan_queue.put((holder, _hold_lane))
    assert holding.wait(timeout=5)

    ran = threading.Event()
    queued = jm.Job(title="queued scan")
    queued.kind = "scan"
    jm.registry.add(queued)
    jm._scan_queue.put((queued, lambda j: ran.set()))

    assert queued.status == jm.JobStatus.PENDING
    assert jm.request_cancel(queued) is True
    assert queued.status == jm.JobStatus.CANCELED
    assert queued not in jm.registry.pending_and_running()

    release.set()
    assert _wait_for(lambda: holder.status == jm.JobStatus.DONE)
    assert not ran.wait(timeout=0.5)
    assert queued.status == jm.JobStatus.CANCELED


def test_post_job_hook_reports_reviews_and_results_without_the_app_secrets(
        tmp_path, monkeypatch):
    """A scan parked for review is reported, not only finished jobs, and the
    approved picks run. The hook gets the job's JSON but none of the app's
    secrets in its environment."""
    jm.start_worker()
    executed = {}
    hooks = []
    monkeypatch.setattr(
        jm, "_start_post_job_hook",
        lambda payload: payload["id"] == job.id and hooks.append(payload))

    def scan(j):
        j.add_candidate("album", "Album A", "Artist", payload={"id": 1})
        j.add_candidate("album", "Album B", "Artist", payload={"id": 2})

    def execute(j, chosen):
        executed["ids"] = [c["payload"]["id"] for c in chosen]

    job = jm.Job(title="scan")
    jm.submit_scan(job, scan, execute)
    assert _wait_for(lambda: job.status == jm.JobStatus.AWAITING_REVIEW)
    assert len(job.candidates) == 2
    assert _wait_for(lambda: hooks)
    assert (hooks[0]["status"], hooks[0]["review_items"]) == (
        "awaiting_review", 2)
    assert jm.approve(job, ["c1"])
    assert _wait_for(lambda: job.status == jm.JobStatus.DONE)
    assert executed["ids"] == [2]
    assert _wait_for(lambda: len(hooks) == 2)
    assert hooks[1]["status"] == "done"

    sink = tmp_path / "hook.json"
    payload = {
        "id": "local-hook-check",
        "status": "done",
        "title": "Album",
    }
    monkeypatch.setenv("HOOK_SINK", str(sink))
    monkeypatch.setenv("WEB_AUTH_PASSWORD", "seeded web password")
    monkeypatch.setenv("QOBUZ_USER_AUTH_TOKEN_FILE", "/run/secrets/qobuz_token")
    monkeypatch.setenv("LASTFM_API_KEY", "lastfm key")
    monkeypatch.setenv(
        "POST_JOB_HOOK", 'tee "$HOOK_SINK"; env | cut -d= -f1 > "$HOOK_SINK.env"')

    jm._run_post_job_hook(payload)

    assert sink.read_text() == (
        '{"id": "local-hook-check", "status": "done", "title": "Album"}'
    )
    names = set((tmp_path / "hook.json.env").read_text().split())
    assert "HOOK_SINK" in names
    assert not names & {
        "WEB_AUTH_PASSWORD", "QOBUZ_USER_AUTH_TOKEN_FILE", "LASTFM_API_KEY"}


def test_a_new_job_replaces_or_joins_only_one_with_the_same_intent(monkeypatch):
    """A per-artist rescan supersedes only that artist's parked review. A
    /download folds onto an in-flight job only for the same intent: "get this
    edition too" is a deliberate extra copy and a one-track grab is its own
    thing, neither swallowed by an unrelated job for the same album, and a
    full-album download does not fold onto a one-track grab."""
    from qobuz_librarian.web import download_admission, job_persistence, runtime, scans

    monkeypatch.setattr(job_persistence, "persist", lambda _job: True)
    monkeypatch.setattr(
        job_persistence, "ready_for_admission", lambda: True)

    class Authority:
        @staticmethod
        def intact():
            return True

    monkeypatch.setattr(runtime, "_RUN_LOCK_HANDLE", Authority())
    monkeypatch.setattr(runtime, "_JOBS_RESTORED", True)

    def _park(artist):
        j = jm.Job(title="Artist scan", artist=artist)
        j.execute_kind = "album"
        j.status = jm.JobStatus.AWAITING_REVIEW
        jm.registry.add(j)
        return j

    a = _park("Artist A")
    b = _park("Artist B")
    noop_scan, noop_exec = (lambda j: None), (lambda j, chosen: None)

    fresh = jm.Job(title="Artist scan", artist="Artist C")
    fresh.execute_kind = "album"
    scans._submit_scan_deduped(fresh, noop_scan, noop_exec, "album")
    assert a.status == jm.JobStatus.AWAITING_REVIEW
    assert b.status == jm.JobStatus.AWAITING_REVIEW

    rescan = jm.Job(title="Artist scan", artist="Artist A")
    rescan.execute_kind = "album"
    scans._submit_scan_deduped(rescan, noop_scan, noop_exec, "album")
    assert a.status == jm.JobStatus.CANCELED
    assert b.status == jm.JobStatus.AWAITING_REVIEW
    for job in (a, b, fresh, rescan):
        _remove_job(job)

    full = jm.Job(title="Album X", artist="Artist", album_id="X")
    full.status = jm.JobStatus.RUNNING
    jm.registry.add(full)

    assert download_admission._duplicate_download_job("X") is full
    assert download_admission._duplicate_download_job("X", as_new_edition=True) is None
    assert download_admission._duplicate_download_job("X", track_id="42") is None
    # ...but two identical "this edition too" taps are one download.
    full.execute_args = {"new_edition": True}
    assert download_admission._duplicate_download_job("X", as_new_edition=True) is full

    grab = jm.Job(title="One track", artist="Artist", album_id="Y")
    grab.single = {"album_id": "Y", "track_id": "7"}
    grab.status = jm.JobStatus.RUNNING
    jm.registry.add(grab)

    assert download_admission._duplicate_download_job("Y", track_id="7") is grab
    assert download_admission._duplicate_download_job("Y", track_id="8") is None
    assert download_admission._duplicate_download_job("Y") is None

    # A parked review's candidate is a proposal, not a queued download.
    review = jm.Job(title="Library scan")
    review.status = jm.JobStatus.AWAITING_REVIEW
    review.add_candidate(kind="album", title="Z", artist="Artist", payload={"album_id": "Z"})
    jm.registry.add(review)
    assert download_admission._duplicate_download_job("Z") is None


def test_a_download_rechecks_the_music_root_and_ownership_before_it_runs(
        client, monkeypatch, tmp_path):
    """A queued download stops if the music folder is no longer the one it
    was queued for, and a "this edition too" download checks again whether
    that exact edition is already owned."""
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as catalog_mod
    from qobuz_librarian.library import collection_snapshot
    from qobuz_librarian.web import flows, job_runs, qobuz_access

    with monkeypatch.context() as patch:
        patch.setattr(
            collection_snapshot,
            "music_root_write_state",
            lambda: ("recorded_empty", 7),
        )
        patch.setattr(
            flows,
            "build_args",
            lambda: (_ for _ in ()).throw(
                AssertionError("download preparation must not start")),
        )
        run = job_runs._make_download_run(
            {"id": "new-album", "artist": {"name": "Artist"},
             "title": "Album", "tracks": {"items": []}},
            "token",
        )

        with pytest.raises(RuntimeError):
            run(jm.Job(title="Waiting download"))

    album = {
        "id": "remaster",
        "title": "Variance",
        "artist": {"name": "The Lab"},
        "release_date_original": "2024-01-01",
        "tracks_count": 2,
        "tracks": {"items": [
            {"id": "remaster-1", "title": "Track 1"},
            {"id": "remaster-2", "title": "Track 2"},
        ]},
    }
    folder = [None]

    monkeypatch.setattr(qobuz_access, "_get_token", lambda: "tok")
    monkeypatch.setattr(
        search_mod, "get_album", lambda _album_id, _token: album)
    monkeypatch.setattr(
        catalog_mod,
        "find_album_dir_filesystem",
        lambda _album, **_kw: folder[0],
    )
    monkeypatch.setattr(
        catalog_mod,
        "find_existing_tracks",
        lambda _album, album_dir=None: (
            list(album["tracks"]["items"]), album_dir),
    )
    monkeypatch.setattr(
        catalog_mod,
        "compute_missing",
        lambda wanted, _existing: ([], list(wanted)),
    )

    submitted = []
    monkeypatch.setattr(
        job_runs,
        "_make_download_run",
        lambda *_args, **_kwargs: (lambda _job: None),
    )
    monkeypatch.setattr(
        jm,
        "submit",
        lambda job, _run: submitted.append(job) or job,
    )
    headers = {"HX-Request": "true"}
    absent = client.post(
        "/download",
        data={"album_id": "remaster", "as_new_edition": "1"},
        headers=headers,
    )
    assert absent.headers["X-QL-Download-Outcome"] == "queued"
    assert submitted[0].execute_args == {"new_edition": True}

    remaster = tmp_path / "Variance (2024)"
    remaster.mkdir()
    folder[0] = remaster
    stale = client.post(
        "/download",
        data={"album_id": "remaster", "as_new_edition": "1"},
        headers=headers,
    )
    assert stale.headers["X-QL-Download-Outcome"] == "owned"
    assert len(submitted) == 1


# ── app.py: HTTP routes ───────────────────────────────────────────────────────


class _SameThreadASGIClient:
    """Small sync wrapper around HTTPX's ASGI transport.

    Starlette's TestClient uses a cross-thread AnyIO portal. That portal can
    hang in some local Python environments before the app sees a request, so
    these route tests drive the async FastAPI routes on the calling thread.
    """

    def __init__(self, app):
        self.app = app
        self.base_url = "http://testserver"
        self.cookies = httpx.Cookies()
        self.headers = httpx.Headers()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def request(self, method, url, **kwargs):
        extra_headers = kwargs.pop("headers", None)
        headers = httpx.Headers(self.headers)
        if extra_headers:
            headers.update(extra_headers)
        follow_redirects = kwargs.pop("follow_redirects", True)

        async def _send():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url=self.base_url,
                cookies=self.cookies,
                headers=headers,
                follow_redirects=follow_redirects,
            ) as ac:
                response = await ac.request(method, url, **kwargs)
                self.cookies.update(ac.cookies)
                return response

        return asyncio.run(_send())

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def stream(self, method, url, **kwargs):
        return _ResponseContext(self.request(method, url, **kwargs))


class _ResponseContext:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, *_exc):
        try:
            self.response.close()
        except RuntimeError:
            pass
        return False


class _InlineExecutorLoop:
    async def run_in_executor(self, _executor, fn, *args):
        return fn(*args)


class _InlineExecutorAsyncio:
    def __init__(self, real_asyncio):
        self._real_asyncio = real_asyncio

    def get_running_loop(self):
        return _InlineExecutorLoop()

    def __getattr__(self, name):
        return getattr(self._real_asyncio, name)


def _run_web_executors_inline(monkeypatch):
    from qobuz_librarian.web import (
        hidden_pages,
        qobuz_access,
        routes_api,
        routes_auth,
        routes_backup,
        routes_discover,
        routes_downsample,
        routes_jobs,
        routes_library,
        routes_queue,
        routes_repair,
        routes_search,
        routes_settings,
        routes_upgrade,
        scans,
    )

    for module in (qobuz_access, hidden_pages, scans, routes_api, routes_auth,
                   routes_backup, routes_discover, routes_downsample, routes_jobs,
                   routes_library, routes_queue, routes_repair, routes_search,
                   routes_settings, routes_upgrade):
        monkeypatch.setattr(module, "asyncio", _InlineExecutorAsyncio(asyncio))


@pytest.fixture
def client(monkeypatch):
    from qobuz_librarian.api.auth import credentials_from_values
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )
    from qobuz_librarian.web import app as app_mod
    from qobuz_librarian.web import job_persistence, qobuz_access, queue_recovery, runtime

    class TestAuthority:
        def __init__(self):
            self.closed = False

        def intact(self):
            return not self.closed

        def close(self):
            self.closed = True

    monkeypatch.setattr(runtime, "_RUN_LOCK_HANDLE", TestAuthority())
    monkeypatch.setattr(runtime, "_CLI_MODE", False)
    monkeypatch.setattr(runtime, "_LOCK_BUSY_PID", None)
    monkeypatch.setattr(runtime, "_LOCK_UNENFORCEABLE", False)
    monkeypatch.setattr(runtime, "_SHUTTING_DOWN", False)
    # This lightweight client bypasses the application lifespan. Treat its
    # in-memory registry and persistence gate as ready unless a test exercises
    # either startup path.
    monkeypatch.setattr(runtime, "_JOBS_RESTORED", True)
    monkeypatch.setattr(
        job_persistence, "ready_for_admission", lambda: True)
    clear_recovery = StartupRecoveryResult(StartupRecoveryStatus.CLEAR)
    monkeypatch.setattr(queue_recovery, "_STARTUP_RECOVERY_RESULT", clear_recovery)
    monkeypatch.setattr(queue_recovery, "_STARTUP_RECOVERY_UNKNOWN", False)
    qobuz_credentials = credentials_from_values(
        "test-user",
        "test-token",
        source="streamrip",
    )

    async def _authorize_for_web(*_args, **_kwargs):
        return qobuz_credentials

    monkeypatch.setattr(qobuz_access, "_authorize_qobuz_for_web", _authorize_for_web)
    monkeypatch.setattr(
        qobuz_access,
        "_authorize_qobuz_live",
        lambda *_args, **_kwargs: qobuz_credentials,
    )
    monkeypatch.setattr(
        qobuz_access,
        "_credential_generation_is_active",
        lambda generation: generation == qobuz_credentials.generation,
    )

    def _record_clear(_authority):
        queue_recovery._STARTUP_RECOVERY_RESULT = clear_recovery
        return clear_recovery

    monkeypatch.setattr(queue_recovery, "_record_startup_recovery", _record_clear)
    _run_web_executors_inline(monkeypatch)
    with _SameThreadASGIClient(app_mod.app) as c:
        c.get("/queue")
        token = c.cookies.get("ql_csrf")
        c.headers.update({"X-CSRF-Token": token})
        yield c


def test_health_separates_liveness_from_readiness(client, monkeypatch,
                                                   tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import auth as web_auth
    from qobuz_librarian.web import job_persistence, storage

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", data_dir)
    monkeypatch.setenv("WEB_AUTH", "on")
    monkeypatch.setattr(
        web_auth, "creds_file_present_but_unreadable", lambda: False)
    monkeypatch.setattr(storage, "_unwritable_volumes", lambda: [])
    monkeypatch.setattr(job_persistence, "_disabled", False)
    monkeypatch.setattr(job_persistence, "_schema_ready", True)
    monkeypatch.setattr(job_persistence, "_admission_ready", True)
    monkeypatch.setattr(job_persistence, "_conn", None)

    assert client.get("/healthz").json() == {"ok": True}
    assert client.request("HEAD", "/healthz").status_code == 200
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "ready"}
    assert client.request("HEAD", "/readyz").status_code == 200

    monkeypatch.setattr(
        web_auth, "creds_file_present_but_unreadable", lambda: True)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"] == ["credentials"]
    assert client.get("/healthz").status_code == 200

    monkeypatch.setattr(
        web_auth, "creds_file_present_but_unreadable", lambda: False)
    data_dir.rmdir()
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"] == ["data"]
    assert client.request("HEAD", "/readyz").status_code == 503


def test_a_form_address_opened_again_goes_to_its_page(client):
    # A phone restoring a tab opens the address a form posted to, which only
    # takes POST; it showed the framework's bare JSON.
    for form, page in (("/settings/behavior", "/settings"),
                       ("/jobs/abc/approve", "/jobs/abc")):
        response = client.get(form, follow_redirects=False)
        assert (response.status_code, response.headers["location"]) == (303, page)


def test_album_search_marks_each_edition_by_what_the_library_holds(
        client, monkeypatch, tmp_path, tagged_flac):
    """A new mix stays downloadable beside the original it remasters, as an
    album or a track, until its own recordings are on disk. Search leads with
    the original pressing and counts what other masters hold (The Doors put
    out a 25-track "50th Anniversary Deluxe Edition" and two plain 11-track
    pressings of Waiting for the Sun, none of which carries a version field,
    the shape that broke a grouped row). Albums that match only on the
    artist's name are dropped."""
    import html.parser

    import qobuz_librarian.library.catalog as catalog_mod
    from qobuz_librarian import config as cfg
    from qobuz_librarian.api import search
    from qobuz_librarian.library import candidate_premise, scanner
    from qobuz_librarian.modes import process
    from qobuz_librarian.queue import executor
    from qobuz_librarian.web import flows, job_runs, qobuz_access, routes_search

    original = {
        "id": "rubber-soul", "title": "Rubber Soul", "version": "Remastered 2009",
        "artist": {"name": "The Beatles"},
        "release_date_original": "1965-12-03", "tracks_count": 2,
        "maximum_bit_depth": 16,
        "tracks": {"items": [
            {"id": "drive", "title": "Drive My Car", "version": "Remastered",
             "isrc": "GBAYE0601479"},
            {"id": "norwegian", "title": "Norwegian Wood (This Bird Has Flown)",
             "version": "Remastered", "isrc": "GBAYE0601480"},
        ]},
    }
    remix = {**original, "id": "rubber-soul-2026", "version": "2026 Mix",
             "tracks": {"items": [
                 {**track, "id": f"{track['id']}-2026", "version": "2026 Mix", "isrc": isrc}
                 for track, isrc in zip(original["tracks"]["items"],
                                        ("GBUM72600368", "GBUM72600369"))
             ]}}
    albums = {album["id"]: album for album in (remix, original)}
    beatles = tmp_path / "beatles"
    monkeypatch.setattr(cfg, "MUSIC_ROOT", beatles)
    folder = beatles / "The Beatles" / "Rubber Soul (1965)"
    folder.mkdir(parents=True)
    for track in original["tracks"]["items"]:
        tagged_flac(folder / f"{track['id']}.flac", TITLE=f"{track['title']} (Remastered)",
                    ALBUM="Rubber Soul", ARTIST="The Beatles", ISRC=track["isrc"])
    scanner.clear_scan_caches()
    monkeypatch.setattr(qobuz_access, "_get_token", lambda: "tok")
    monkeypatch.setattr(search, "get_album", lambda album_id, _token: albums[album_id])
    monkeypatch.setattr(flows, "get_album", lambda album_id, _token: albums[album_id])
    monkeypatch.setattr(search, "qobuz_get", lambda *_args, **_kwargs: {
        "albums": {"items": list(albums.values())},
    })

    results, _groups = asyncio.run(routes_search._album_results(
        list(albums.values()), "Rubber Soul", "tok", set(), set()))
    assert {r["id"]: r["owned"] for r in results} == {
        "rubber-soul": True, "rubber-soul-2026": False,
    }
    assert all(t["owned"] for t in routes_search._album_tracklist(original["id"], "tok"))
    assert not any(t["owned"] for t in routes_search._album_tracklist(remix["id"], "tok"))

    class DownloadForms(html.parser.HTMLParser):
        def __init__(self, markup):
            super().__init__()
            self.forms = []
            self.current = None
            self.feed(markup)

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "form" and "data-search-download-form" in attrs:
                self.current = {}
                self.forms.append(self.current)
            elif tag == "input" and self.current is not None:
                self.current[attrs.get("name")] = attrs.get("value", "")

        def handle_endtag(self, tag):
            if tag == "form":
                self.current = None

    headers = {"HX-Request": "true"}
    response = client.post(
        "/search", data={"q": "Rubber Soul", "kind": "album"}, headers=headers)
    forms = DownloadForms(response.text).forms
    assert len(forms) == 2  # Table and grid offer the same separate copy.
    assert all(form["album_id"] == remix["id"] and form.get("as_new_edition") == "1"
               for form in forms)

    submitted = []
    monkeypatch.setattr(job_runs, "_make_download_run", lambda *_a, **_kw: lambda _j: None)
    runs = []

    def submit(job, run):
        submitted.append(job)
        runs.append(run)
        return job

    monkeypatch.setattr(jm, "submit", submit)
    queued = client.post("/download", data=forms[0], headers=headers)
    assert queued.headers["X-QL-Download-Outcome"] == "queued"
    assert submitted[0].execute_args == {"new_edition": True}

    wanted_track = remix["tracks"]["items"][0]
    track_results = asyncio.run(routes_search._track_results(
        [{**wanted_track, "album": remix}], "Drive My Car", "tok", set()))
    assert not track_results[0]["owned"]
    queued_track = client.post("/download", data={
        "album_id": remix["id"], "track_id": wanted_track["id"],
    }, headers=headers)
    assert queued_track.headers["X-QL-Download-Outcome"] == "queued"
    captured = []
    monkeypatch.setattr(executor, "_execute_download_queue", lambda queue, *_a, **_kw: captured.extend(queue))
    runs[1](submitted[1])
    assert captured[0]["missing"] == [wanted_track]
    assert captured[0]["force_track_by_track"]

    partial = folder.with_name("Rubber Soul (2026 Mix) (1965)")
    tagged_flac(partial / "drive.flac", TITLE="Drive My Car (2026 Mix)",
                ALBUM="Rubber Soul (2026 Mix)", ARTIST="The Beatles", ISRC=wanted_track["isrc"])
    scanner.clear_scan_caches()
    folded = []
    planned = []

    def stop_download(**kwargs):
        planned.append((kwargs["album_dir"], kwargs["missing"]))
        raise RuntimeError("download interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(flows, "refold_into_living_review",
                      lambda specs, **_kw: folded.extend(specs) or True)
        patch.setattr(process, "staging_preflight", lambda _args: None)
        patch.setattr(process, "run_album_download", stop_download)
        assert flows._fold_partial_gap_fill(remix, "The Beatles", 1)
        candidate = folded[0]
        job = jm.Job("library", "Retry", execute_kind="library")
        flows.execute_albums(job, [candidate], "tok")
        assert planned == [(partial, [remix["tracks"]["items"][1]])]

    restored = flows._refresh_restored_missing_spec(
        flows._album_candidate_spec(remix, "The Beatles"), "tok")
    premise = candidate_premise.validate(restored)
    assert premise["kind"] == "gap-fill" and Path(premise["path"]) == partial
    assert restored["payload"]["exact_edition"]

    old_folder = folder.with_name("Rubber Soul (1966)")
    folder.rename(old_folder)
    kept = folder
    kept.mkdir()
    for track in remix["tracks"]["items"]:
        tagged_flac(kept / f"{track['id']}.flac", TITLE=f"{track['title']} (2026 Mix)",
                    ALBUM="Rubber Soul", ARTIST="The Beatles", ISRC=track["isrc"])
    scanner.clear_scan_caches()
    results, _groups = asyncio.run(routes_search._album_results(
        list(albums.values()), "Rubber Soul", "tok", set(), set()))
    assert all(r["owned"] for r in results)
    assert all(t["owned"] for t in routes_search._album_tracklist(remix["id"], "tok"))
    track_results = asyncio.run(routes_search._track_results(
        [{**wanted_track, "album": remix}], "Drive My Car", "tok", set()))
    assert track_results[0]["owned"]
    stale = client.post("/download", data=forms[0], headers=headers)
    assert stale.headers["X-QL-Download-Outcome"] == "owned"
    assert len(submitted) == 2

    def album(album_id, title, year, tracks):
        return {"id": album_id, "title": title, "artist": {"name": "The Doors"},
                "release_date_original": f"{year}-07-03", "tracks_count": len(tracks),
                "maximum_bit_depth": 16, "tracks": {"items": tracks}}

    hello = {"id": "hello", "title": "Hello, I Love You", "isrc": "USEE10000001"}
    love = {"id": "love", "title": "Love Street", "isrc": "USEE10000002"}
    deluxe = album("deluxe", "Waiting for the Sun (50th Anniversary Deluxe Edition)", 1968,
                   [hello, love, {"id": "demo", "title": "Not to Touch the Earth (Demo)",
                                  "isrc": "USEE11800003"}])
    reissue = album("reissue", "Waiting for the Sun", 1985, [hello, love])
    original = album("original", "Waiting for the Sun", 1968, [hello, love])
    albums = {a["id"]: a for a in (deluxe, reissue, original)}
    doors = tmp_path / "doors"
    monkeypatch.setattr(cfg, "MUSIC_ROOT", doors)
    folder = doors / "The Doors" / "Waiting for the Sun (1968)"
    tagged_flac(folder / "01.flac", TITLE="Hello, I Love You", ALBUM="Waiting for the Sun",
                ARTIST="The Doors", ISRC=hello["isrc"], TRACKNUMBER="1")
    tagged_flac(folder / "02.flac", TITLE="Love Street (2018 Remaster)",
                ALBUM="Waiting for the Sun", ARTIST="The Doors", ISRC="USRH11800002",
                TRACKNUMBER="2")
    scanner.clear_scan_caches()
    monkeypatch.setattr(search, "get_album", lambda album_id, _token: albums[album_id])

    results, groups = asyncio.run(routes_search._album_results(
        list(albums.values()), "Waiting for the Sun", "tok", set(), set()))
    rows = {r["id"]: r for r in results}
    assert rows["original"]["as_new_edition"] and not rows["original"].get("partial")
    assert (rows["deluxe"]["have_tracks"], rows["deluxe"]["want_tracks"]) == (2, 3)
    [group] = groups
    assert group["id"] == "original"
    assert [e["id"] for e in group["others"]] == ["deluxe", "reissue"]
    assert (group["other_have"], group["other_want"]) == (2, 3)

    monkeypatch.setattr(qobuz_access, "_get_token", lambda: "tok")
    monkeypatch.setattr(catalog_mod, "find_album_dir_filesystem", lambda _a, **_kw: None)
    monkeypatch.setattr(search, "qobuz_get", lambda *_a, **_kw: {
        "albums": {"items": [
            {"id": "crystal-castles-iii", "title": "(III)",
             "artist": {"name": "Crystal Castles"}},
            {"id": "stan-hubbs-crystal", "title": "Crystal",
             "artist": {"name": "Stan Hubbs"}},
        ]},
    })

    response = client.post(
        "/search", data={"q": "Crystal", "kind": "album"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert 'data-search-key="album-stan-hubbs-crystal"' in response.text
    assert 'data-search-key="album-crystal-castles-iii"' not in response.text


def test_long_artist_list_shows_owned_albums_before_the_rest_loads(
        client, monkeypatch, tmp_path, tagged_flac):
    """An artist with hundreds of albums lists them a batch at a time. An
    album the library holds far down the catalogue is in the first batch, the
    batch that reaches it leaves it out, and a pick kept from before a reload
    pulls in every batch up to it."""
    import html
    import re

    from qobuz_librarian import config as cfg
    from qobuz_librarian.api import search
    from qobuz_librarian.library import scanner
    from qobuz_librarian.web import qobuz_access

    albums = [{"id": f"rec-{n}", "title": f"Record {n}",
               "artist": {"name": "The Testers"},
               "release_date_original": "2001-01-01", "tracks_count": 1,
               "maximum_bit_depth": 16,
               "tracks": {"items": [{"id": f"song-{n}", "title": f"Song {n}",
                                     "isrc": f"QZTST01{n:05d}"}]}}
              for n in range(130)]
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path)
    tagged_flac(tmp_path / "The Testers" / "Record 120 (2001)" / "song.flac",
                TITLE="Song 120", ALBUM="Record 120", ARTIST="The Testers",
                ISRC="QZTST0100120")
    scanner.clear_scan_caches()
    monkeypatch.setattr(qobuz_access, "_get_token", lambda: "tok")
    monkeypatch.setattr(search, "get_artist_albums",
                        lambda *_a, **_kw: (albums, len(albums)))
    monkeypatch.setattr(search, "get_album",
                        lambda album_id, _token: albums[int(album_id[4:])])

    def listed(response):
        return set(re.findall(r'data-search-key="album-(rec-\d+)"', response.text))

    def next_batch(response):
        found = re.search(r'data-search-more-url="([^"]+)"', response.text)
        return html.unescape(found.group(1)) if found else None

    def records(numbers):
        return {f"rec-{n}" for n in numbers if n != 120}

    headers = {"HX-Request": "true"}
    first = client.post("/search", data={
        "q": "The Testers", "kind": "artist", "artist_id": "7",
        "artist_name": "The Testers"}, headers=headers)
    assert listed(first) == records(range(50)) | {"rec-120"}
    assert 'data-search-key="album-rec-120" data-owned="1"' in first.text

    second = client.get(next_batch(first), headers=headers)
    assert listed(second) == records(range(50, 100))
    last = client.get(next_batch(second), headers=headers)
    assert listed(last) == records(range(100, 130))
    assert next_batch(last) is None

    through = client.get(next_batch(first) + "&through=rec-104", headers=headers)
    assert listed(through) == records(range(50, 130))


def test_settings_saves_wait_for_running_jobs_and_never_drop_another_change(
        tmp_path, monkeypatch):
    """An in-flight job must not see cfg.* flip mid-run. Two saves at once
    both land. The terminal saved a new download quality while the web app
    stayed open, and the next web Settings save put the old quality back."""
    import threading

    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "deferred.json")
    monkeypatch.setattr(cfg, "DOWNSAMPLE_HIRES_ENABLED", False)
    monkeypatch.setattr(ss, "_any_active_job", lambda: True)
    monkeypatch.setattr(ss, "_pending_apply", None)

    ok, _ = ss.save({"DOWNSAMPLE_HIRES_ENABLED": True})
    assert ok is True
    assert (tmp_path / "deferred.json").exists()
    assert cfg.DOWNSAMPLE_HIRES_ENABLED is False  # not yet applied

    ss.drain_pending()
    assert cfg.DOWNSAMPLE_HIRES_ENABLED is True
    ss.drain_pending()
    assert cfg.DOWNSAMPLE_HIRES_ENABLED is True  # idempotent

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(ss, "_any_active_job", lambda: False)
    monkeypatch.setattr(cfg, "LYRICS_ENABLED", True)
    monkeypatch.setattr(cfg, "PREFER_HIRES", True)
    with ss._pending_lock:
        ss._pending_apply = None
    start = threading.Barrier(3)
    outcomes = []

    def save_one(values):
        start.wait()
        outcomes.append(ss.save(values)[0])

    threads = [
        threading.Thread(target=save_one, args=({"LYRICS_ENABLED": False},)),
        threading.Thread(target=save_one, args=({"PREFER_HIRES": False},)),
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert not any(thread.is_alive() for thread in threads)
    assert outcomes == [True, True]
    assert json.loads((tmp_path / "s.json").read_text()) == {
        "LYRICS_ENABLED": False,
        "PREFER_HIRES": False,
    }
    assert cfg.LYRICS_ENABLED is False
    assert cfg.PREFER_HIRES is False

    store = tmp_path / "terminal.json"
    monkeypatch.setattr(ss, "SETTINGS_FILE", store)
    monkeypatch.setattr(ss, "_pending_apply", None)
    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 4)
    monkeypatch.setitem(ss._ENV_DEFAULTS, "STREAMRIP_QUALITY", 4)
    shown = ss.current()
    generation = ss.form_generation("behaviour", shown)

    store.write_text(json.dumps({"STREAMRIP_QUALITY": "3"}))
    posted = {key: shown[key] for key in ss.FORM_KEYS["behaviour"]}
    posted["LYRICS_ENABLED"] = not shown["LYRICS_ENABLED"]
    with pytest.raises(ss.SettingsChanged):
        ss.save_from_form(posted, "behaviour", generation)

    assert json.loads(store.read_text()) == {"STREAMRIP_QUALITY": "3"}
    ss.refresh()
    assert ss.current()["STREAMRIP_QUALITY"] == "3"


def test_a_setting_back_on_its_compose_value_is_not_pinned(tmp_path, monkeypatch):
    """Emptying a text field means "use the environment again", not "save a
    blank". Saving the blank pinned it, so the Compose value was gone for good
    and the settings file had to be edited by hand to get it back. A switch
    turned off and on again, and lyric providers saved unchanged or in
    another spelling, stop overriding Compose too."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    store = tmp_path / "s.json"
    monkeypatch.setattr(ss, "SETTINGS_FILE", store)
    monkeypatch.setattr(cfg, "BEETS_PATH_DEFAULT", "$albumartist/$album")
    monkeypatch.setitem(ss._ENV_DEFAULTS, "BEETS_PATH_DEFAULT",
                        "$albumartist/$album")
    monkeypatch.setattr(ss, "_any_active_job", lambda: False)
    monkeypatch.setattr(ss, "_pending_apply", None)

    ok, _ = ss.save({"BEETS_PATH_DEFAULT": "Mine/$album"})
    assert ok is True
    assert cfg.BEETS_PATH_DEFAULT == "Mine/$album"

    ok, _ = ss.save({"BEETS_PATH_DEFAULT": ""})
    assert ok is True
    assert cfg.BEETS_PATH_DEFAULT == "$albumartist/$album"
    assert "BEETS_PATH_DEFAULT" not in json.loads(store.read_text())

    # A store already carrying a pinned blank recovers on the next start.
    store.write_text(json.dumps({"BEETS_PATH_DEFAULT": ""}))
    ss.load()
    assert cfg.BEETS_PATH_DEFAULT == "$albumartist/$album"
    assert "BEETS_PATH_DEFAULT" not in json.loads(store.read_text())

    monkeypatch.setitem(ss._ENV_DEFAULTS, "LYRICS_ENABLED", True)
    ok, _ = ss.save({"LYRICS_ENABLED": False})
    assert ok is True and json.loads(store.read_text()) == {"LYRICS_ENABLED": False}
    ok, _ = ss.save({"LYRICS_ENABLED": True})
    assert ok is True and json.loads(store.read_text()) == {}

    providers = tmp_path / "providers.json"
    monkeypatch.setattr(ss, "SETTINGS_FILE", providers)
    monkeypatch.setattr(cfg, "LYRICS_PROVIDERS", ["lrclib", "netease"])
    monkeypatch.setitem(ss._ENV_DEFAULTS, "LYRICS_PROVIDERS", ["lrclib", "netease"])
    with ss._pending_lock:
        ss._pending_apply = None

    ok, _ = ss.save({"LYRICS_PROVIDERS": ss.current()["LYRICS_PROVIDERS"]})
    assert ok
    assert "LYRICS_PROVIDERS" not in (ss._read_settings() or {})

    providers.write_text('{"LYRICS_PROVIDERS": "Lrclib,NetEase"}')
    ss.load()
    assert "LYRICS_PROVIDERS" not in (ss._read_settings() or {})


def test_settings_retry_keeps_edits_and_custom_timer_values(client, tmp_path, monkeypatch):
    import html.parser

    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    class Forms(html.parser.HTMLParser):
        def __init__(self, markup):
            super().__init__()
            self.forms = []
            self.current = None
            self.select = None
            self.feed(markup)

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "form" and attrs.get("action") == "/settings/behavior":
                self.current = {}
                self.forms.append(self.current)
            elif self.current is not None:
                if tag == "input" and attrs.get("name"):
                    if attrs.get("type") == "checkbox":
                        if "checked" not in attrs:
                            return
                        self.current[attrs["name"]] = attrs.get("value", "on")
                    else:
                        self.current[attrs["name"]] = attrs.get("value", "")
                elif tag == "select":
                    self.select = attrs["name"]
                elif tag == "option" and self.select:
                    if "selected" in attrs or self.select not in self.current:
                        self.current[self.select] = attrs["value"]

        def handle_endtag(self, tag):
            if tag == "form":
                self.current = None
            elif tag == "select":
                self.select = None

    store = tmp_path / "s.json"
    monkeypatch.setattr(ss, "SETTINGS_FILE", store)
    monkeypatch.setattr(ss, "_pending_apply", None)
    for key, value in (("NEW_RELEASE_CHECK_INTERVAL", 3600),
                       ("ARTIST_CATALOG_CACHE_TTL", 0),
                       ("LYRICS_ENABLED", True)):
        monkeypatch.setattr(cfg, key, value)
        monkeypatch.setitem(ss._ENV_DEFAULTS, key, value)

    page = client.get("/settings")
    assert page.status_code == 200
    form = next(data for data in Forms(page.text).forms if "form_complete" in data)
    del form["LYRICS_ENABLED"]
    path = form["BEETS_PATH_DEFAULT"]
    form["BEETS_PATH_DEFAULT"] = "/music/$albumartist/$album"
    rejected = client.post("/settings/behavior", data=form, follow_redirects=False)
    assert rejected.status_code == 200
    retained = next(data for data in Forms(rejected.text).forms if "form_complete" in data)
    assert retained == form
    assert cfg.LYRICS_ENABLED is True
    assert not store.exists()
    retained["BEETS_PATH_DEFAULT"] = path
    form = retained
    response = client.post("/settings/behavior", data=form, follow_redirects=False)

    assert response.status_code == 303
    assert cfg.LYRICS_ENABLED is False
    assert cfg.NEW_RELEASE_CHECK_INTERVAL == 3600
    assert cfg.ARTIST_CATALOG_CACHE_TTL == 0
    assert json.loads(store.read_text()) == {"LYRICS_ENABLED": False}
    # A field saved before stays saved when the form posts it unchanged.
    client.post("/settings/behavior", data=form, follow_redirects=False)
    assert json.loads(store.read_text()) == {"LYRICS_ENABLED": False}


# ── run-lock busy → destructive routes 503, read-only stay open ───────


def test_web_writes_pause_while_the_terminal_holds_the_library(client, monkeypatch):
    """With the run lock held elsewhere every destructive route answers 503
    without naming the lock or its pid, and read-only pages stay open. Handing
    the library to the terminal pauses downloads until web mode resumes."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import qobuz_access, runtime

    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", True, raising=False)
    monkeypatch.setattr(qobuz_access, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(qobuz_access, "_TOKEN_VALID", True)
    with monkeypatch.context() as patch:
        patch.setattr(runtime, "_LOCK_BUSY_PID", 4321)

        dash = client.get("/")
        assert dash.status_code == 200
        assert "4321" not in dash.text

        for path, data in [
            ("/download", {"album_id": "1"}),
            ("/library", {}),
            ("/downsample", {}),
            ("/repair", {}),
            ("/lyrics", {}),
            ("/lyric-retry", {}),
            ("/jobs/whatever/approve", {}),
        ]:
            r = client.post(path, data=data, follow_redirects=False)
            assert r.status_code == 503, f"{path} should 503 when lock busy"
            assert "pid 4321" not in r.text
            assert "run-lock" not in r.text

    # No active job (the registry is a shared singleton across tests).
    monkeypatch.setattr(jm.registry, "pending_and_running",
                        lambda: [])
    r = client.post("/settings/mode", data={"target": "cli"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings?mode=cli#mode"
    assert runtime._CLI_MODE is True
    blocked = client.post("/download", data={"album_id": "123"},
                          follow_redirects=False)
    assert blocked.status_code == 503
    back = client.post("/settings/mode", data={"target": "web"},
                       follow_redirects=False)
    assert back.status_code == 303 and back.headers["location"] == "/settings?mode=web#mode"
    assert runtime._CLI_MODE is False


@pytest.mark.parametrize(
    ("execute_kind", "path"),
    [
        ("library", "/library"),
        ("upgrade", None),
    ],
)
def test_a_saved_review_survives_qobuz_being_unavailable(
        client, monkeypatch, execute_kind, path):
    """A saved review stays visible, blocked, while Qobuz is unreachable, and
    an approval Qobuz rejects leaves the exact review in place with nothing
    queued."""
    from qobuz_librarian.api.auth import AuthLost
    from qobuz_librarian.web import qobuz_access, saved_reviews

    job = jm.Job(
        title=f"{execute_kind} saved review",
        kind="scan",
        execute_kind=execute_kind,
        status=jm.JobStatus.AWAITING_REVIEW,
    )
    job.add_candidate(
        "album",
        "Saved Album",
        "Saved Artist",
        payload={"album_id": "saved"},
        selected=True,
    )
    jm.registry.add(job)
    destination = path or f"/jobs/{job.id}"
    with monkeypatch.context() as patch:
        patch.setattr(qobuz_access, "_read_creds", lambda: {})
        patch.setattr(qobuz_access, "_qobuz_ready", lambda: False)
        try:
            response = client.get(destination)

            assert response.status_code == 200
            assert "Saved Album" in response.text
            assert 'data-review-blocked="1"' in response.text
            assert 'id="review-submit"' in response.text
        finally:
            _remove_job(job)

    async def rejected(*_args, **_kwargs):
        raise AuthLost("rejected")

    job = jm.Job(title="Saved review")
    job.kind = "scan"
    job.execute_kind = execute_kind
    job.status = jm.JobStatus.AWAITING_REVIEW
    job._execute_fn = lambda _job, _chosen: None
    job.execute_args = {
        "quality_signature": saved_reviews._effective_upgrade_quality_signature(),
    }
    job.add_candidate(
        "album",
        "A",
        "X",
        payload={"album_id": "a1"},
        selected=True,
    )
    jm.registry.add(job)
    before = copy.deepcopy((job.candidates, job.execute_args, job.status))
    queued = []
    monkeypatch.setattr(qobuz_access, "_authorize_qobuz_for_web", rejected)
    monkeypatch.setattr(
        saved_reviews,
        "_sync_saved_review_before_approve",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("review mutation preceded Qobuz preflight")
        ),
    )
    monkeypatch.setattr(jm._scan_queue, "put", queued.append)
    try:
        response = client.post(
            f"/jobs/{job.id}/approve",
            data={"tab": "missing"} if execute_kind == "library" else {},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]
        assert (job.candidates, job.execute_args, job.status) == before
        assert queued == []
    finally:
        _remove_job(job)


def test_upgrade_saved_review_respects_hidden_candidates_and_clears_its_dot(
        client, monkeypatch, tmp_path):
    """Hiding an artist in the saved Upgrade review drops its rows from the
    review and the count, reopening reuses the same review, and opening
    Upgrade clears its review dot."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden
    from qobuz_librarian.web import job_persistence, qobuz_access, review_badges, saved_reviews
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    monkeypatch.setattr(cfg, "REVIEW_BADGE_STATE_FILE",
                        tmp_path / "review-badges.json")
    monkeypatch.setattr(job_persistence, "_write_snapshot", lambda *_a: True)
    monkeypatch.setattr(qobuz_access, "_get_token", lambda: "tok")
    monkeypatch.setattr(qobuz_access, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    state = {
            "updated_at": time.time(),
            "complete": True,
            "quality_signature": saved_reviews._effective_upgrade_quality_signature(),
            "candidates": [
                {
                    "title": "Dummy",
                    "artist": "Portishead",
                    "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                    "payload": {"album_id": "up1", "year": "1994", "cover": ""},
                },
                {
                    "title": "Third",
                    "artist": "Portishead",
                    "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                    "payload": {"album_id": "up2", "year": "2008", "cover": ""},
                },
            ],
        }
    _make_saved_surface_current(monkeypatch, "upgrade", state)
    monkeypatch.setattr(
        "qobuz_librarian.quality.upgrade_state.load", lambda: state)

    first = client.post("/upgrade/review", follow_redirects=False)
    job_id = first.headers["location"].removeprefix("/jobs/")
    job = job_mgr.registry.get(job_id)
    keep = next(c["cid"] for c in job.candidates if c["title"] == "Dummy")
    client.post(f"/jobs/{job.id}/select", data={"cid": keep, "checked": "1"})
    client.post(f"/jobs/{job.id}/hide", data={"artist": "Portishead"})

    store = hidden.load()
    assert hidden.is_hidden(hidden.SCOPE_UPGRADE, "Portishead", "Third", store)
    assert [c["title"] for c in job.candidates] == ["Dummy"]

    review_badges.mark_ready("upgrade")
    r = client.get("/upgrade")
    assert r.status_code == 200
    assert review_badges.snapshot()["upgrade"] is False
    assert saved_reviews._upgrade_state_summary()["count"] == 1

    second = client.post("/upgrade/review", follow_redirects=False)
    assert second.headers["location"] == first.headers["location"]
    assert len([
        j for j in job_mgr.registry.awaiting_review()
        if j.execute_kind == "upgrade"
    ]) == 1


def test_duplicate_qobuz_approval_queues_once(client, monkeypatch):
    from qobuz_librarian.web import flows

    monkeypatch.setattr(
        "qobuz_librarian.library.candidate_premise.validate_all",
        lambda _candidates: [],
    )
    monkeypatch.setattr(
        "qobuz_librarian.library.candidate_premise.stale_candidate_ids",
        lambda _candidates, **_kw: set(),
    )

    job = jm.Job(title="Library review")
    job.kind = "scan"
    job.execute_kind = "library"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job._execute_fn = lambda _job, _chosen: None
    job.add_candidate(
        "album",
        "A",
        "X",
        payload={"album_id": "a1"},
        selected=True,
    )
    jm.registry.add(job)
    queued = []
    monkeypatch.setattr(
        flows,
        "owned_missing_candidate_ids",
        lambda *_args, **_kwargs: set(),
    )
    monkeypatch.setattr(jm._scan_queue, "put", queued.append)
    try:
        first = client.post(
            f"/jobs/{job.id}/approve",
            data={"tab": "missing"},
            follow_redirects=False,
        )
        second = client.post(
            f"/jobs/{job.id}/approve",
            data={"tab": "missing"},
            follow_redirects=False,
        )

        assert first.headers["location"].startswith("/library?approved=1")
        assert second.headers["location"].startswith("/library?stale=1")
        assert len(queued) == 1
    finally:
        _remove_job(job)


def test_auth_loss_before_any_import_reparks_the_review_or_fails_cleanly(
        monkeypatch):
    """Qobuz dying on the FIRST album of an approved run must not consume the
    review: the picks go back to awaiting-review instead of a failed job. A
    split review whose put-back cannot be saved fails instead of hanging, as
    the failed save is logged while the job's lock is held and the job's own
    log handler takes that lock again."""
    from qobuz_librarian.api.auth import AuthLost
    from qobuz_librarian.web import job_persistence

    job = jm.Job(title="Library scan")
    job.kind = "scan"
    job.execute_kind = "library"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job.add_candidate("album", "A", "X", payload={"album_id": "a1"})

    def _dies(j, chosen):
        raise AuthLost("token rejected")

    job._execute_fn = _dies
    jm.registry.add(job)
    try:
        jm.start_worker()
        assert jm.approve(job, None) is True
        assert _wait_for(lambda: job.status == jm.JobStatus.AWAITING_REVIEW)
        assert job.candidates[0]["selected"]
        assert job.finished_at is None
        assert job.error is None
    finally:
        _remove_job(job)

    monkeypatch.setattr(job_persistence, "_warned_write_failure", False)
    broken = sqlite3.connect(":memory:", check_same_thread=False)

    job = jm.Job(title="Library scan")
    job.kind = "scan"
    job.execute_kind = "library"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job.add_candidate("album", "A", "X", payload={"album_id": "a1"})
    remnant = jm.Job(title="Library scan")
    remnant.kind = "scan"
    remnant.execute_kind = "library"
    remnant.status = jm.JobStatus.AWAITING_REVIEW

    def _dies_unsaved(j, chosen):
        monkeypatch.setattr(job_persistence, "_get_conn", lambda: broken)
        raise AuthLost("token rejected")

    job._execute_fn = _dies_unsaved
    jm.registry.add(job)
    try:
        assert jm.approve(job, None, split_review=lambda _job: remnant) is True
        assert _wait_for(lambda: job.status == jm.JobStatus.FAILED)
    finally:
        _remove_job(job)
        _remove_job(remnant)


def test_failed_automatic_scan_resume_is_not_restarted_on_the_next_visit(
    monkeypatch,
):
    """An unfinished first Library scan that fails when Search resumes it
    waits before the next automatic resume instead of starting on every
    visit."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import generation_state, scan_checkpoint
    from qobuz_librarian.web import (
        new_release_checks,
        qobuz_access,
        routes_search,
        scans,
        write_gate,
    )

    monkeypatch.setattr(routes_search, "_auto_scan",
                        {"job": None, "until": 0.0, "delay": 0.0})
    monkeypatch.setattr(cfg, "AUTO_LIBRARY_SCAN", True)
    monkeypatch.setattr(write_gate, "_web_writes_paused", lambda: False)
    monkeypatch.setattr(qobuz_access, "_qobuz_ready", lambda: True)
    monkeypatch.setattr(generation_state, "baseline_complete", lambda *_a: False)
    monkeypatch.setattr(scan_checkpoint, "pending",
                        lambda: {"kind": "missing", "done": 3})
    monkeypatch.setattr(new_release_checks, "_auto_start_credentials", object)
    started = []

    def failing_scan(_credentials, partial_only=False):
        job = jm.Job(title="Library scan", status=jm.JobStatus.FAILED,
                     finished_at=time.time())
        started.append(job)
        return job

    monkeypatch.setattr(scans, "_start_library_scan", failing_scan)
    routes_search._maybe_resume_library_scan()
    routes_search._maybe_resume_library_scan()
    assert len(started) == 1

    # The refresh a folder readable again starts waits out the same failure.
    from qobuz_librarian.library import unreadable_artists
    monkeypatch.setattr(generation_state, "baseline_complete", lambda *_a: True)
    monkeypatch.setattr(unreadable_artists, "load", lambda: ["Artist"])
    monkeypatch.setattr(unreadable_artists, "readable_again", lambda _l: True)
    monkeypatch.setattr(routes_search, "_unreadable_checked_at", -1e9)
    routes_search._maybe_resume_library_scan()
    assert len(started) == 1


def test_held_downloads_wait_for_qobuz_or_recovery_and_resume_in_order(
        monkeypatch):
    """A token Qobuz rejects before a download starts holds that download and
    every one queued behind it, instead of failing each on its own. Downloads
    that reach staging while another download's interrupted recovery pauses
    the library wait for it to settle the same way."""
    from qobuz_librarian.api.auth import AuthLost
    from qobuz_librarian.web import job_runs, qobuz_access, settings_store

    requeued = []

    def rejected(_job):
        raise jm.WaitForQobuz() from AuthLost("rejected")

    first = jm.Job(title="A", album_id="a1")
    second = jm.Job(title="B", album_id="b1")
    with monkeypatch.context() as patch:
        patch.setattr(jm, "_held_downloads", [])
        patch.setattr(jm, "_qobuz_hold", None)
        patch.setattr(jm, "_held_release_check", lambda: True)
        patch.setattr(qobuz_access, "_recheck_token_if_due", lambda: None)
        patch.setattr(
            job_runs, "_requeued_download_run",
            lambda job: lambda j: requeued.append(j.id))
        try:
            jm.start_worker()
            jm.submit(first, rejected)
            jm.submit(second, rejected)
            assert _wait_for(lambda: (jm.qobuz_hold() or {}).get("waiting") == 2)
            assert first.status is second.status is jm.JobStatus.PENDING
            assert not requeued
            assert not settings_store._any_active_job()

            jm.release_qobuz_hold()
            assert _wait_for(lambda: requeued == [first.id, second.id])
        finally:
            _remove_job(first)
            _remove_job(second)

    blocked = [True]
    ran = []
    monkeypatch.setattr(jm, "_held_downloads", [])
    monkeypatch.setattr(jm, "_held_release_check", lambda: not blocked[0])
    monkeypatch.setattr(jm, "_staging_entry_guard", lambda _job: not blocked[0])

    def run(job):
        with jm.staging_lock():
            ran.append(job.id)

    first = jm.Job(title="A", album_id="a1")
    second = jm.Job(title="B", album_id="b1")
    try:
        jm.submit(first, run)
        jm.submit(second, run)
        assert _wait_for(lambda: [job for job, _run in jm._held_downloads]
                         == [first, second])
        assert first.status is second.status is jm.JobStatus.PENDING

        blocked[0] = False
        assert _wait_for(lambda: ran == [first.id, second.id])
    finally:
        _remove_job(first)
        _remove_job(second)


def test_first_downsample_prompts_for_keep_choice_then_saves_it(
        client, monkeypatch, tmp_path):
    """With keep-originals still unchosen, approving a downsample shows the
    one-time prompt instead of rewriting anything; picking one saves it to the
    real setting and the run proceeds, so a returning user is never asked again."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import flows
    from qobuz_librarian.web import jobs as job_mgr
    from qobuz_librarian.web import settings_store as ss

    _allow_legacy_candidate_execution(monkeypatch)

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(ss, "_pending_apply", None)
    monkeypatch.setattr(cfg, "DOWNSAMPLE_KEEP_ORIGINALS", None)
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(job_mgr._scan_queue, "put", lambda item: None)
    state = {
        "updated_at": time.time(), "complete": True,
        "candidates": [{
            "title": "Album", "artist": "Portishead",
            "detail": "24-bit / 96 kHz -> 16-bit / 48 kHz",
            "album_dir": "/music/Portishead/Album", "est_saving": 1234,
        }],
    }
    _make_saved_surface_current(monkeypatch, "downsample", state)
    monkeypatch.setattr(
        "qobuz_librarian.library.downsample_state.load", lambda: state)

    first = client.post("/downsample/review", follow_redirects=False)
    job = job_mgr.registry.get(first.headers["location"].removeprefix("/jobs/"))
    client.post(f"/jobs/{job.id}/select",
                data={"cid": job.candidates[0]["cid"], "checked": "1"})

    r = client.post(
        f"/jobs/{job.id}/approve",
        data={"downsample_policy": ""},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert 'name="downsample_policy" value=""' in r.text
    assert job.status == job_mgr.JobStatus.AWAITING_REVIEW
    assert cfg.DOWNSAMPLE_KEEP_ORIGINALS is None

    real_write = ss._atomic_write_settings
    monkeypatch.setattr(ss, "_atomic_write_settings", lambda _data: False)
    failed = client.post(
        f"/jobs/{job.id}/approve",
        data={"keep_choice": "keep", "downsample_policy": ""},
        follow_redirects=False,
    )
    assert failed.status_code == 303
    assert "error=" in failed.headers["location"]
    assert job.status == job_mgr.JobStatus.AWAITING_REVIEW
    assert cfg.DOWNSAMPLE_KEEP_ORIGINALS is None

    monkeypatch.setattr(ss, "_atomic_write_settings", real_write)
    # A download in the other worker lane defers global settings application.
    # This approval must still carry the saved keep policy into its own
    # destructive run instead of reading the old None as "delete".
    monkeypatch.setattr(ss, "_any_active_job", lambda: True)
    r2 = client.post(
        f"/jobs/{job.id}/approve",
        data={"keep_choice": "keep", "downsample_policy": ""},
        follow_redirects=False,
    )
    assert r2.status_code == 303
    assert cfg.DOWNSAMPLE_KEEP_ORIGINALS is None
    assert ss.current()["DOWNSAMPLE_KEEP_ORIGINALS"] == "keep"
    assert job.execute_args["keep_originals"] is True

    received = []
    monkeypatch.setattr(
        flows,
        "execute_downsamples",
        lambda _job, _chosen, **kwargs: received.append(
            kwargs["keep_originals"]
        ),
    )
    job._execute_fn(job, job.selected_candidates())
    assert received == [True]


def test_retry_rebuilds_archived_failed_download(client, monkeypatch):
    from qobuz_librarian.api.auth import QobuzUnavailable
    from qobuz_librarian.web import job_persistence, qobuz_access, track_downloads

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()

    archived = jm.Job(
        title="Roads",
        artist="Portishead",
        album_id="al1",
        edition="Live Version",
    )
    archived.single = {"album_id": "al1", "track_id": "roads-live"}
    archived.status = jm.JobStatus.FAILED
    archived.finished_at = time.time() - 10
    job_persistence.persist(archived)
    with sqlite3.connect(job_persistence._path()) as observer:
        observer.execute(
            "UPDATE jobs SET log_lines='{', quality_shortfall='[]' "
            "WHERE id=?",
            (archived.id,),
        )

    detail = client.get(f"/jobs/{archived.id}")
    assert detail.status_code == 200
    assert archived.title in detail.text

    sibling = jm.Job(title="Sour Times", album_id="al1")
    sibling.single = {"album_id": "al1", "track_id": "sour-times-live"}
    jm.registry.add(sibling)
    queued = []
    monkeypatch.setattr(jm._download_queue, "put", queued.append)

    monkeypatch.setattr(qobuz_access, "_get_token", lambda: "tok")
    outage = {"active": True}

    def get_album(_album_id, _token):
        if outage["active"]:
            raise QobuzUnavailable("request deadline exhausted")
        return {
            "title": "Dummy",
            "version": "Anniversary Edition",
            "artist": {"name": "Portishead"},
            "tracks": {"items": [{
                "id": "roads-live",
                "title": "Roads",
                "version": "Live Version",
            }]},
        }

    monkeypatch.setattr(
        "qobuz_librarian.api.search.get_album",
        get_album,
    )
    seen = {}

    def single_run(album, track, token):
        seen["track_id"] = track["id"]
        return lambda job: None

    monkeypatch.setattr(track_downloads, "_make_single_track_run", single_run)

    jobs_before = {item.id for item in jm.registry.all()}
    r = client.post(f"/jobs/{archived.id}/retry", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"].startswith("/queue?error=")
    assert job_persistence.load_one(archived.id)["status"] == "failed"
    assert {item.id for item in jm.registry.all()} == jobs_before

    outage["active"] = False
    # Only History may steer the redirect; an off-site address is ignored.
    r = client.post(f"/jobs/{archived.id}/retry",
                    data={"return_to": "https://example.invalid/steal"},
                    follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"].startswith("/jobs/")
    new_id = r.headers["location"].removeprefix("/jobs/")
    assert new_id and new_id != archived.id
    new_job = jm.registry.get(new_id)
    assert new_job is not None and new_job.album_id == "al1"
    assert seen == {"track_id": "roads-live"}
    assert new_job.single == {"album_id": "al1", "track_id": "roads-live"}
    assert new_job.edition == "Live Version"
    assert new_job.display_title == "Roads (Live Version)"
    assert job_persistence.load_one(new_id)["edition"] == "Live Version"
    repeated = client.post(f"/jobs/{archived.id}/retry", follow_redirects=False)
    assert repeated.headers["location"] == f"/jobs/{new_id}"
    assert len(queued) == 1
    _remove_job(sibling)
    _remove_job(new_job)


def test_an_unfinished_queue_holds_only_downloads_until_given_up(
    client, monkeypatch, tmp_path,
):
    """A Library walk stopped with an album queued paused every web scan and
    tool, while the terminal holds only its downloads for that queue. A rip
    that fails before writing anything leaves its saved queue entry waiting
    on Retry, which pauses every download and scan and fails the same way
    each time; Give up has to clear it."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian import run_lock
    from qobuz_librarian.queue import journal
    from qobuz_librarian.queue.builder import _build_queue_item
    from qobuz_librarian.web import job_persistence, queue_recovery, runtime, write_gate

    def _record(lease):
        result = queue_recovery._recover_startup_queue(lease)
        queue_recovery._STARTUP_RECOVERY_RESULT = result
        return result

    monkeypatch.setattr(cfg, "LOCK_FILE", tmp_path / "run.lock")
    monkeypatch.setattr(cfg, "STAGING_DIR", tmp_path / "walk-staging")
    monkeypatch.setattr(cfg, "QUEUE_JOURNAL_DIR", tmp_path / "walk-journals")
    item = _build_queue_item(
        album={"id": "42", "title": "Anvil Vapre"}, album_dir=None,
        label="Anvil Vapre", missing=[], present=[],
        upgrade_only=False, auto_upgrade=False,
    )
    journal.save_pending_queue([item], mode="walk_queue")

    authority = run_lock.acquire()
    monkeypatch.setattr(runtime, "_RUN_LOCK_HANDLE", authority)
    monkeypatch.setattr(queue_recovery, "_record_startup_recovery", _record)
    try:
        _record(authority)
        assert not write_gate._web_writes_paused()
        assert write_gate._web_downloads_paused()
        assert queue_recovery._staging_entry_allowed(
            jm.Job(title="Lyrics scan", execute_kind="lyrics"))
        assert not queue_recovery._staging_entry_allowed(
            jm.Job(title="Anvil Vapre", album_id="7"))

        r = client.post("/download", data={"album_id": "7"},
                        follow_redirects=False)
        assert r.status_code == 503
    finally:
        authority.close()

    monkeypatch.setattr(cfg, "STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(cfg, "QUEUE_JOURNAL_DIR", tmp_path / "journals")
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()

    job = jm.Job(title="Anvil Vapre", artist="Autechre", album_id="42")
    job.status = jm.JobStatus.FAILED
    job.finished_at = time.time()
    jm.registry.add(job)
    job_persistence.persist(job)
    track = {"id": "101", "media_number": 1, "track_number": 1}
    item = _build_queue_item(
        album={"id": "42", "title": "Anvil Vapre", "maximum_bit_depth": 24,
               "maximum_sampling_rate": 96, "tracks": {"items": [track]}},
        album_dir=None, label="Anvil Vapre", missing=[track], present=[],
        upgrade_only=False, auto_upgrade=False, quality=4,
    )
    saved = journal.save_queue_journal(
        journal.create_queue_journal([item], mode=f"web-job:{job.id}"))

    authority = run_lock.acquire()
    monkeypatch.setattr(runtime, "_RUN_LOCK_HANDLE", authority)
    try:
        _record(authority)
        assert queue_recovery._startup_recovery_status_value() == "resume_required"
        control = queue_recovery._durable_recovery_control()

        r = client.post(
            f"/jobs/{job.id}/give-up",
            data={"recovery_operation_id": control["operation_id"],
                  "recovery_item_id": control["item_id"]},
            follow_redirects=False,
        )

        assert r.status_code == 303
        assert (journal.load_queue_journal(saved.operation_id).status
                is journal.QueueLoadStatus.ABSENT)
        assert queue_recovery._startup_recovery_status_value() == "clear"
    finally:
        authority.close()
        _remove_job(job)


# ── per-job cancel button on queue page ───────────────────────────────


def _inject_job(status, title="Test Job"):
    """Add a job directly to the shared registry and return it.
    Caller must remove the job in a finally block."""
    job = jm.Job(title=title, status=status)
    jm.registry.add(job)
    return job


def _remove_job(job):
    with jm.registry._lock:
        jm.registry._jobs.pop(job.id, None)
        try:
            jm.registry._order.remove(job.id)
        except ValueError:
            pass


def test_library_approve_downloads_the_checked_choices_and_parks_the_rest(
        client, monkeypatch, tmp_path):
    """Approving with an old selection token changes nothing, and approving
    Missing albums splits Gap fill off into its own review. Live releases sit
    in the saved review behind their switch: Select all, the tab count and
    Download selected leave the unticked ones out while the switch is off and
    keep them parked for later, and a ticked one stays listed and
    downloads."""
    from qobuz_librarian.web import qobuz_access
    monkeypatch.setattr("qobuz_librarian.config.MISSING_TYPES_FILE",
                        tmp_path / "types.json")
    monkeypatch.setattr(
        "qobuz_librarian.library.candidate_premise.validate_all",
        lambda _candidates: [],
    )
    monkeypatch.setattr(
        "qobuz_librarian.library.candidate_premise.stale_candidate_ids",
        lambda _candidates, **_kw: set(),
    )
    monkeypatch.setattr(qobuz_access, "_read_creds",
                        lambda: {"auth_token": "t", "user_id": "u"})
    monkeypatch.setattr(qobuz_access, "_TOKEN_VALID", True)
    monkeypatch.setattr(jm._scan_queue, "put", lambda item: None)

    def split_review(job):
        return next(j for j in jm.registry.all()
                    if j is not job and j.execute_kind == "library"
                    and j.status == jm.JobStatus.AWAITING_REVIEW)

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.review_verb = "Download"
    job._execute_fn = lambda j, chosen: None
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"album_id": "third", "year": "2008"},
                      selected=True)
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      payload={"album_id": "dummy", "year": "1994",
                               "gap_fill": 2}, selected=True)
    job.add_candidate(kind="album", title="Untrue", artist="Burial",
                      payload={"album_id": "untrue", "year": "2007",
                               "gap_fill": 1}, selected=False)
    split = None
    try:
        old = client.post(f"/jobs/{job.id}/select",
                          data={"cid": "c0", "checked": "0"}).json()
        current = client.post(f"/jobs/{job.id}/select",
                              data={"cid": "c0", "checked": "1"}).json()
        stale = client.post(
            f"/jobs/{job.id}/approve",
            data={"tab": "missing", "selection_token": old["selection_tokens"]["missing"]},
            follow_redirects=False,
        )
        assert stale.status_code == 303
        assert job.status == jm.JobStatus.AWAITING_REVIEW
        assert len(job.candidates) == 3

        r = client.post(f"/jobs/{job.id}/approve",
                        data={"tab": "missing", "selection_token": current["selection_tokens"]["missing"]},
                        follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/library?approved=1"
        assert [c["title"] for c in job.candidates] == ["Third"]
        assert job.status != jm.JobStatus.AWAITING_REVIEW
        split = split_review(job)
        titles = {c["title"]: c["selected"] for c in split.candidates}
        assert titles == {"Dummy": True, "Untrue": False}
        assert split.summary
        assert split._execute_fn is not None
    finally:
        _remove_job(job)
        if split is not None:
            _remove_job(split)

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.review_verb = "Download"
    job._execute_fn = lambda j, chosen: None
    for title, payload in (
            ("Darkness", {"album_id": "dark", "release_type": "album"}),
            ("Live 1975-85", {"album_id": "live", "release_type": "live"}),
            ("Hammersmith Odeon", {"album_id": "odeon", "release_type": "live"}),
            ("Nebraska", {"album_id": "nebraska"})):
        job.add_candidate(kind="album", title=title, artist="Bruce Springsteen",
                          payload=payload, selected=False)
    split = None
    try:
        r = client.post(f"/jobs/{job.id}/select-all",
                        data={"on": "1", "scope": "all", "tab": "missing"}).json()
        assert (r["missing_total"], r["missing_selected"]) == (2, 2)
        r = client.post(f"/jobs/{job.id}/missing-types",
                        data={"type": "live", "on": "1"}).json()
        assert (r["missing_total"], r["missing_selected"]) == (4, 2)
        client.post(f"/jobs/{job.id}/select",
                    data={"cid": job.candidates[1]["cid"], "checked": "1"})
        r = client.post(f"/jobs/{job.id}/missing-types",
                        data={"type": "live", "on": "0"}).json()
        assert (r["missing_total"], r["missing_selected"]) == (3, 3)

        client.post(f"/jobs/{job.id}/approve",
                    data={"tab": "missing",
                          "selection_token": r["selection_tokens"]["missing"]},
                    follow_redirects=False)
        assert sorted(c["title"] for c in job.candidates) == [
            "Darkness", "Live 1975-85", "Nebraska"]
        split = split_review(job)
        assert [(c["title"], c["selected"]) for c in split.candidates] == [
            ("Hammersmith Odeon", False)]
    finally:
        _remove_job(job)
        if split is not None:
            _remove_job(split)


def test_select_all_and_dismiss_rest_act_only_on_the_rows_in_scope(
        client, monkeypatch, tmp_path):
    """With a filter showing 3 rows, Select all must not silently flip the
    other thousand, and Deselect must scope the same way so a filtered
    select-all can be undone filtered. Dismiss unselected hides everything
    unticked in one save: saved per artist, a large review took minutes."""
    from qobuz_librarian.library import hidden
    from qobuz_librarian.web import job_persistence

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=False)
    job.add_candidate(kind="album", title="Ashes", artist="Agalloch",
                      payload={"year": "2006"}, selected=False)
    try:
        r = client.post(f"/jobs/{job.id}/select-all",
                        data={"on": "1", "scope": "all", "tab": "missing",
                              "q": "agalloch"})
        assert r.status_code == 200
        assert r.json()["filtered_total"] == 1
        assert r.json()["filtered_selected"] == 1
        assert r.json()["filtered_rest"] == 0
        flags = {x["title"]: x["selected"] for x in job.candidates}
        assert flags == {"Third": False, "Ashes": True}
        page = client.get(
            f"/jobs/{job.id}/review",
            params={"tab": "missing", "q": "agalloch"},
        )
        assert 'data-filtered-total="1"' in page.text
        assert 'data-filtered-selected="1"' in page.text
        r = client.post(f"/jobs/{job.id}/select-all",
                        data={"on": "0", "scope": "all", "tab": "missing",
                              "q": "agalloch"})
        assert r.json()["filtered_selected"] == 0
        assert r.json()["filtered_rest"] == 1
        # Empty query keeps the whole-tab behavior.
        client.post(f"/jobs/{job.id}/select-all",
                    data={"on": "1", "scope": "all", "tab": "missing", "q": ""})
        flags = {x["title"]: x["selected"] for x in job.candidates}
        assert flags == {"Third": True, "Ashes": True}
    finally:
        _remove_job(job)

    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", tmp_path / "h.json")
    saves = []
    monkeypatch.setattr(job_persistence, "_write_snapshot",
                        lambda *_a: saves.append(1) or True)
    monkeypatch.setattr(jm, "persist_soon", lambda _job: None)

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    keep = job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                             payload={"year": "1994"}, selected=False)
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=False)
    job.add_candidate(kind="album", title="Untrue", artist="Burial",
                      payload={"year": "2007"}, selected=False)
    job.add_candidate(kind="album", title="Mezzanine", artist="Massive Attack",
                      payload={"year": "1998"}, selected=False)
    try:
        r = client.post(f"/jobs/{job.id}/select", data={"cid": keep, "checked": "1"})
        assert r.status_code == 200

        saves.clear()
        r = client.post(f"/jobs/{job.id}/dismiss-rest")
        assert r.status_code == 200
        assert len(saves) == 1
        body = r.json()
        assert body["hidden"] == 3
        assert body["total"] == 1
        assert body["selected"] == 1
        assert body["review_done"] is False

        survivors = {c["artist"] + "/" + c["title"]: c["selected"] for c in job.candidates}
        assert survivors == {"Portishead/Dummy": True}

        store = hidden.load()
        assert not hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Dummy", store)
        assert hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Third", store)
        assert hidden.is_hidden(hidden.SCOPE_MISSING, "Burial", "Untrue", store)
        assert hidden.is_hidden(hidden.SCOPE_MISSING, "Massive Attack", "Mezzanine", store)

        job.execute_args["_candidate_cap_hit"] = True
        job.execute_args["_unchecked_artists"] = 3
        client.post(f"/jobs/{job.id}/select", data={"cid": keep, "checked": "0"})
        r = client.post(f"/jobs/{job.id}/dismiss-rest")
        assert r.status_code == 200
        assert r.json()["review_done"] is True
        assert job.finished_at is not None
        assert str(job.unchecked_artists) in job.summary
    finally:
        _remove_job(job)


def test_a_restart_requeues_waiting_downloads_but_not_the_started_one(
        client, monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import job_persistence, lifespan, runtime, write_gate

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()
    started = jm.Job(title="First", album_id="1", created_at=100.0,
                     status=jm.JobStatus.RUNNING)
    third = jm.Job(title="Third", album_id="3", created_at=300.0)
    second = jm.Job(title="Second", album_id="2", created_at=200.0)
    finished = jm.Job(title="Finished", album_id="4", created_at=50.0)
    for job in (started, third, second, finished):
        job_persistence.persist(job)
    connection = job_persistence._conn
    connection.execute(
        "INSERT INTO durable_job_completions "
        "(job_id, job_created_at, album_id, operation_id, item_id, "
        "completion_hash, acknowledged_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (finished.id, finished.created_at, finished.album_id,
         "completed-import", "album", "a" * 64, 75.0),
    )
    connection.commit()

    class UnavailableRead:
        query = "FROM jobs ORDER BY created_at"

        def execute(self, query, *args):
            if self.query in query:
                raise sqlite3.OperationalError("disk I/O error")
            return connection.execute(query, *args)

        def __getattr__(self, name):
            return getattr(connection, name)

    unavailable = UnavailableRead()
    monkeypatch.setattr(job_persistence, "_conn", unavailable)
    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    monkeypatch.setattr(jm, "_held_downloads", [])
    monkeypatch.setattr(runtime, "_JOBS_RESTORED", False)

    assert lifespan._restore_jobs_once() is False
    assert write_gate._web_writes_paused()
    assert write_gate._readiness_report()[0] == 503
    assert not jm.registry.all()
    assert not jm._held_downloads

    unavailable.query = "FROM durable_job_completions"
    assert lifespan._restore_jobs_once() is False
    assert not jm.registry.all()
    assert not jm._held_downloads

    monkeypatch.setattr(job_persistence, "_conn", connection)
    assert lifespan._restore_jobs_once() is True
    assert lifespan._restore_jobs_once() is True
    assert write_gate._readiness_report()[0] == 200

    assert jm.registry.get(started.id).status == jm.JobStatus.FAILED
    assert jm.registry.get(finished.id).status == jm.JobStatus.DONE
    assert len(jm.registry.all()) == 4
    assert [job.id for job, _run in jm._held_downloads] == [second.id, third.id]
    assert {job.status for job, _run in jm._held_downloads} == {
        jm.JobStatus.PENDING}


def test_a_restart_restores_saved_jobs_past_broken_rows_and_old_schemas(
        monkeypatch, tmp_path):
    """A scan parked for review survives a container restart with its
    candidates and can still be approved, instead of re-scanning from artist
    1. One broken review fails alone instead of aborting the restore. A job
    rebuilt with existing candidates advances its cid counter past them: a
    fresh c0/c1 colliding with inherited rows made a cid-keyed dismiss delete
    unrelated, even ticked, candidates. A download whose own completion is
    durably acknowledged is done, not "Failed / Recovery attention" because
    another item's recovery was outstanding. A database carrying the current
    version stamp but missing a column gets the column, since every persist()
    against it otherwise failed silently."""
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    saved = jm.Job(title="Artist scan", artist="Foo")
    saved.kind = "scan"
    saved.execute_kind = "album"
    saved.status = jm.JobStatus.AWAITING_REVIEW
    saved.add_candidate("album", "Bar", "Foo", payload={"album_id": "abc"})
    job_persistence.persist(saved)

    monkeypatch.setattr(jm, "registry", jm.JobRegistry())

    executed = {}

    def _factory(job, _args):
        return lambda j, chosen: executed.setdefault("ids", [
            c["payload"]["album_id"] for c in chosen])

    jm.restore_jobs({"album": _factory})

    restored = jm.registry.get(saved.id)
    assert restored is not None
    assert restored.status == jm.JobStatus.AWAITING_REVIEW
    assert len(restored.candidates) == 1
    assert restored.candidates[0]["payload"] == {"album_id": "abc"}

    # And the user can still approve: the execute_fn was rebound from the
    # kind registry rather than vanishing with the dead closure.
    jm.start_worker()
    assert jm.approve(restored, ["c0"]) is True
    assert _wait_for(lambda: restored.status == jm.JobStatus.DONE)
    assert executed.get("ids") == ["abc"]

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    broken = jm.Job(title="Broken migration")
    broken.execute_kind = "migration"
    broken.execute_args = {"src": []}
    broken.status = jm.JobStatus.AWAITING_REVIEW
    broken.add_candidate("album", "Dummy", "Portishead", payload={})
    broken_choices = jm.Job(title="Broken Library choices")
    broken_choices.execute_kind = "library"
    broken_choices.status = jm.JobStatus.AWAITING_REVIEW
    broken_choices.add_candidate("album", "Unreadable", payload={})
    healthy = jm.Job(title="Healthy history")
    healthy.status = jm.JobStatus.DONE
    assert job_persistence.persist(broken)
    assert job_persistence.persist(broken_choices)
    assert job_persistence.persist(healthy)
    with sqlite3.connect(job_persistence._path()) as observer:
        observer.execute(
            "UPDATE jobs SET candidates=? WHERE id=?",
            (
                json.dumps([{
                    "cid": "c0",
                    "seq": 0,
                    "kind": "album",
                    "title": "Unreadable",
                    "artist": {"name": "not a review label"},
                    "detail": "",
                    "payload": {},
                    "selected": True,
                }]),
                broken_choices.id,
            ),
        )

    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    sent = []
    monkeypatch.setattr(
        jm,
        "_start_post_job_hook",
        lambda payload: sent.append((payload["id"], payload["status"])),
    )

    def migration_factory(_job, args):
        Path(args["src"])
        return lambda _job, _chosen: None

    jm.restore_jobs({
        "migration": migration_factory,
        "library": lambda _job, _args: lambda _j, _chosen: None,
    })

    restored_broken = jm.registry.get(broken.id)
    assert restored_broken.status == jm.JobStatus.FAILED
    assert restored_broken.error
    restored_choices = jm.registry.get(broken_choices.id)
    assert restored_choices.status == jm.JobStatus.FAILED
    assert restored_choices.error
    assert jm.registry.get(healthy.id).status == jm.JobStatus.DONE
    assert job_persistence.load_one(broken.id)["status"] == "failed"
    assert job_persistence.load_one(broken_choices.id)["status"] == "failed"
    assert sent == [
        (broken.id, "failed"),
        (broken_choices.id, "failed"),
    ]

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    saved = jm.Job(title="Library scan")
    saved.kind = "scan"
    saved.execute_kind = "library"
    saved.status = jm.JobStatus.AWAITING_REVIEW
    saved.candidates = [
        {"cid": "c57", "seq": 57, "kind": "album", "title": "A", "artist": "X",
         "detail": "", "payload": {}, "selected": True},
        # A legacy row persisted before seq existed, recovered from the cid.
        {"cid": "c656", "kind": "album", "title": "B", "artist": "Y",
         "detail": "", "payload": {}, "selected": False},
    ]
    job_persistence.persist(saved)
    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    jm.restore_jobs({"library": lambda job, args: (lambda j, chosen: None)})

    restored = jm.registry.get(saved.id)
    restored.add_candidate("album", "C", "Z")
    restored.add_candidate("album", "D", "W")
    cids = [c["cid"] for c in restored.candidates]
    assert len(set(cids)) == len(cids)
    assert restored.candidates[-1]["seq"] > 656

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    saved = jm.Job(title="Burial, Distant Lights", artist="Burial")
    saved.kind = "download"
    saved.album_id = "abc123"
    saved.status = jm.JobStatus.RUNNING
    job_persistence.persist(saved)

    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    monkeypatch.setattr(job_persistence, "durable_completion_acknowledged",
                        lambda job_id, **_kw: True)

    jm.restore_jobs({}, durable_recovery_clear=False,
                    durable_recovery_job_id=None)

    restored = jm.registry.get(saved.id)
    assert restored.status == jm.JobStatus.DONE
    assert restored.attention == ""

    db = tmp_path / "jobs.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT NOT NULL "
        "DEFAULT '', artist TEXT NOT NULL DEFAULT '', album_id TEXT NOT NULL "
        "DEFAULT '', kind TEXT NOT NULL DEFAULT 'download', status TEXT NOT "
        "NULL, phase TEXT NOT NULL DEFAULT '', candidates TEXT NOT NULL "
        "DEFAULT '[]', error TEXT, summary TEXT NOT NULL DEFAULT '', "
        "review_verb TEXT NOT NULL DEFAULT 'Download', execute_kind TEXT NOT "
        "NULL DEFAULT '', execute_args TEXT NOT NULL DEFAULT '{}', created_at "
        "REAL, finished_at REAL)")
    con.execute(f"PRAGMA user_version = {job_persistence._SCHEMA_VERSION}")
    con.commit()
    con.close()

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    monkeypatch.setattr("qobuz_librarian.config.DATA_DIR", tmp_path)
    job_persistence.init()

    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    con.close()
    assert {"single", "attention", "recoveries", "log_lines",
            "quality_shortfall", "edition"} <= cols


def test_library_review_rebuilds_from_saved_state_when_no_live_job(
        monkeypatch, tmp_path):
    """With the baseline complete but no live library job (swept cancel,
    discarded scan job, corrupt restart row), the Missing Albums / Gap Fill
    review must rebuild from saved scan state, never a finished status with
    no tabs. Retiring the review (discard / worked-through) blocks the rebuild.
    While the terminal holds the library the review is reported as waiting,
    not complete."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import library_scan_state
    from qobuz_librarian.web import routes_library, write_gate

    monkeypatch.setattr(cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "scan.json")
    monkeypatch.setattr(write_gate, "_web_writes_paused", lambda: False)
    library_scan_state.save_kind(
        "missing",
        artists={
            "Agalloch": {
                "fingerprint": "fp",
                "artist_id": "a1",
                "catalog_ids": [],
                "candidates": [
                    {
                        "kind": "album",
                        "title": "The Mantle",
                        "artist": "Agalloch",
                        "detail": "2002 · fully missing",
                        "payload": {"album_id": "m1"},
                    },
                    {
                        "kind": "album",
                        "title": "Ashes",
                        "artist": "Agalloch",
                        "detail": "gap-fill: 2 missing",
                        "payload": {"album_id": "m2", "gap_fill": 2},
                    },
                ],
            },
        },
        complete=True,
    )
    job = None
    try:
        assert routes_library._saved_review_waiting()
        job = routes_library._review_job_from_library_state()
        assert job is not None
        assert job.execute_kind == "library"
        assert job.status == jm.JobStatus.AWAITING_REVIEW
        assert {c["title"] for c in job.candidates} == {"The Mantle", "Ashes"}
        assert all(not c["selected"] for c in job.candidates)
        # Retire it (as a discard / empty would) → no rebuild from stale state.
        _remove_job(job)
        job = None
        library_scan_state.mark_review_retired(now=time.time() + 60)
        assert routes_library._review_job_from_library_state() is None
        assert not routes_library._saved_review_waiting()
    finally:
        if job is not None:
            _remove_job(job)


def test_a_review_run_lands_each_pick_and_reparks_what_it_could_not_finish(
        tmp_path, monkeypatch):
    """One artist's first download must not stale its next missing album. A
    whole review retires successes and re-parks failures for retry, and an
    upgrade run that loses Qobuz after its first success re-parks the albums
    it never started."""
    from copy import deepcopy
    from types import SimpleNamespace

    from qobuz_librarian import config as cfg
    from qobuz_librarian.api.auth import AuthLost
    from qobuz_librarian.library import candidate_premise, library_scan_state
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows, job_persistence

    with monkeypatch.context() as patch:
        music = tmp_path / "music"
        artist_dir = music / "Artist"
        existing = artist_dir / "Existing"
        existing.mkdir(parents=True)
        (existing / "01.flac").write_bytes(b"existing audio")
        patch.setattr(cfg, "MUSIC_ROOT", music)
        patch.setattr(cfg, "ARTIST_API_DELAY", 0)
        patch.setattr(cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "review.json")
        premise = candidate_premise.capture("missing", artist_dir)
        assert premise is not None

        candidates = []
        for album_id in ("First", "Second"):
            candidates.append({
                "kind": "album", "title": album_id, "artist": "Artist",
                "payload": {
                    "album_id": album_id,
                    "_artist_dir_path": str(artist_dir),
                    "_premise": deepcopy(premise),
                },
                "selected": True,
            })
        artists = {"Artist": {"candidates": candidates}}
        assert library_scan_state.save_kind("missing", artists=artists, complete=True)
        job = jm.Job(title="Library run", status=jm.JobStatus.RUNNING)
        job.execute_kind = "library"
        saved = library_scan_state.kind_state("missing")["artists"]["Artist"]
        for candidate in saved["candidates"]:
            flows._add_candidate_spec(job, candidate)
        patch.setattr(flows, "build_args", lambda: SimpleNamespace())
        patch.setattr(
            flows,
            "get_album",
            lambda album_id, _token: {
                "id": album_id,
                "title": album_id,
                "artist": {"name": "Artist"},
            },
        )
        landed = []

        def process_album(album, *_args, **_kwargs):
            album_dir = artist_dir / album["title"]
            album_dir.mkdir()
            (album_dir / "01.flac").write_bytes(b"new audio")
            landed.append(album["id"])
            return {
                "imported": True,
                "n_ok": 1,
                "n_fail": 0,
                "result": "downloaded",
                "dir": str(album_dir),
            }

        patch.setattr(process_mod, "process_album", process_album)
        patch.setattr(
            flows, "_refresh_after_local_album_change", lambda *_a, **_k: None)
        patch.setattr(
            flows, "prune_library_review_candidates", lambda *_a, **_k: 0)

        flows.execute_albums(job, list(job.candidates), "token")

        assert landed == ["First", "Second"]
        assert job.status is not jm.JobStatus.FAILED and not job.error

    monkeypatch.setattr(cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "whole-review.json")
    _allow_legacy_candidate_execution(monkeypatch)

    original = library_scan_state.load()
    running = _inject_job(jm.JobStatus.RUNNING, "Library scan")
    running.execute_kind = "library"
    running._consumed_whole_review = True   # set by _split_and_approve at approve
    running.add_candidate(kind="album", title="Downloaded OK", artist="Agalloch",
                          payload={"album_id": "ok1"}, selected=True)
    running.add_candidate(kind="album", title="Failed One", artist="Agalloch",
                          payload={"album_id": "fail1"}, selected=True)
    chosen = list(running.candidates)
    monkeypatch.setattr(flows.cfg, "ARTIST_API_DELAY", 0)
    monkeypatch.setattr(flows, "get_album", lambda aid, _t: {"id": aid})
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "_refresh_after_local_album_change",
                        lambda *a, **k: None)
    monkeypatch.setattr(flows, "prune_library_review_candidates", lambda *a, **k: 0)

    def fake_process(full, *_a, **_k):
        if full["id"] == "fail1":
            return {"result": "error", "imported": False, "n_ok": 0}
        return {"imported": True, "n_ok": 1, "n_fail": 0, "result": "downloaded",
                "dir": str(tmp_path)}

    monkeypatch.setattr(process_mod, "process_album", fake_process)
    parked = None
    try:
        flows.execute_albums(running, chosen, "tok")
        assert running.status == jm.JobStatus.FAILED
        assert running.summary
        assert running.error
        # The worked-through review is retired → the rebuild won't resurrect it.
        assert library_scan_state.load().get("review_retired_reason") == "worked_through"
        # The failure is re-parked, ticked; the successful download is NOT.
        reviews = [j for j in jm.registry.awaiting_review()
                   if getattr(j, "execute_kind", "") == "library"
                   and any((c.get("payload") or {}).get("album_id") == "fail1"
                           for c in j.candidates)]
        assert len(reviews) == 1
        parked = reviews[0]
        assert {c["title"]: c["selected"] for c in parked.candidates} == {
            "Failed One": True}
    finally:
        library_scan_state._write_state(original)
        _remove_job(running)
        if parked is not None:
            _remove_job(parked)

    monkeypatch.setattr(job_persistence, "_write_snapshot", lambda *_a: True)

    def get_album(album_id, _token):
        if album_id == "second":
            raise AuthLost("expired")
        return {"id": album_id, "title": album_id}

    monkeypatch.setattr(flows, "get_album", get_album)
    upgraded = []

    def process_album(album, *_args, **_kwargs):
        upgraded.append(album["id"])
        return {
            "imported": True,
            "n_ok": 1,
            "result": "downloaded",
            "dir": tmp_path,
        }

    monkeypatch.setattr(process_mod, "process_album", process_album)
    job = jm.Job(title="Upgrade run", status=jm.JobStatus.RUNNING)
    job.execute_kind = "upgrade"
    job.execute_args = {
        "quality_signature": flows.upgrade_state.quality_signature(),
    }
    for title in ("first", "second", "third"):
        job.add_candidate(
            "upgrade",
            title,
            "Artist",
            payload={"album_id": title},
            selected=True,
        )
    parked = None
    try:
        with pytest.raises(AuthLost):
            flows.execute_upgrades(job, list(job.candidates), "token")

        parked = next(
            candidate
            for candidate in jm.registry.awaiting_review()
            if candidate.execute_kind == "upgrade"
        )
        assert {
            candidate["title"]: candidate["selected"]
            for candidate in parked.candidates
        } == {"second": True, "third": True}
        assert upgraded == ["first"]
        assert job.summary and not job.error
    finally:
        if parked is not None:
            _remove_job(parked)


def test_bulk_cancel_pending_never_touches_parked_reviews(client, monkeypatch):
    """Bulk cancellation leaves reviews and protected recovery untouched."""
    monkeypatch.setattr(jm.job_persistence, "_write_snapshot", lambda *_a: True)
    review = jm.Job(title="Library scan")
    review.execute_kind = "library"
    review.status = jm.JobStatus.AWAITING_REVIEW
    review.add_candidate("album", "Keep me", "X", payload={})
    queued = jm.Job(title="Album", artist="A", album_id="q1")
    queued.status = jm.JobStatus.PENDING
    recovery = jm.Job(id="durable-owner", title="Interrupted album retry", artist="A")
    recovery.status = jm.JobStatus.RUNNING
    jm.registry.add(review)
    jm.registry.add(queued)
    jm.registry.add(recovery)
    jm.set_durable_recovery_job_id(recovery.id)
    try:
        individual = client.post(f"/jobs/{recovery.id}/cancel", follow_redirects=False)
        assert individual.status_code == 303
        assert recovery.status == jm.JobStatus.RUNNING
        assert recovery.cancel_requested is False

        queue_refusal = client.post(
            f"/jobs/{recovery.id}/cancel",
            data={"return_to": "/queue"},
            follow_redirects=False,
        )
        assert queue_refusal.status_code == 303
        assert queue_refusal.headers["location"].startswith("/queue?error=")

        bulk = client.post("/queue/cancel-pending", follow_redirects=False)
        assert bulk.status_code == 303
        assert bulk.headers["location"].startswith("/queue")
        assert review.status == jm.JobStatus.AWAITING_REVIEW
        assert len(review.candidates) == 1
        assert queued.cancel_requested is True
        assert recovery.status == jm.JobStatus.RUNNING
        assert recovery.cancel_requested is False
    finally:
        jm.set_durable_recovery_job_id(None)
        _remove_job(review)
        _remove_job(queued)
        _remove_job(recovery)


# ── CLI/web mode hand-off ───────────────────────────────────────────────────────


def test_shutdown_keeps_run_lock_until_workers_and_direct_writes_settle(
        monkeypatch):
    import threading

    from qobuz_librarian.web import lifespan, runtime

    worker_joined = threading.Event()
    release_worker = threading.Event()

    class Worker:
        def is_alive(self):
            return True

        def join(self):
            worker_joined.set()
            release_worker.wait(timeout=5)

    class Handle:
        closed = False

        def intact(self):
            return not self.closed

        def close(self):
            self.closed = True

    handle = Handle()
    stop_event = threading.Event()
    monkeypatch.setattr(jm, "_download_worker_thread", Worker())
    monkeypatch.setattr(jm, "_scan_worker_thread", None)
    monkeypatch.setattr(jm, "_stop_event", stop_event)
    monkeypatch.setattr(jm, "_library_operations_accepting", True)
    monkeypatch.setattr(runtime, "_RUN_LOCK_HANDLE", handle)

    operation = jm.begin_library_operation("Restore")
    assert operation is not None
    shutdown = threading.Thread(target=lifespan._shutdown_web_mutations)
    shutdown.start()
    assert worker_joined.wait(timeout=2)
    assert handle.closed is False
    assert jm.begin_library_operation("Late write") is None

    release_worker.set()
    shutdown.join(timeout=0.1)
    assert shutdown.is_alive()
    assert handle.closed is False

    jm.end_library_operation(operation)
    shutdown.join(timeout=2)
    assert not shutdown.is_alive()
    assert handle.closed is True
    assert runtime._RUN_LOCK_HANDLE is None


# ── web/auth.py: optional login ────────────────────────────────────────────────


def _enable_auth(monkeypatch, tmp_path, *, configure=True):
    """Turn auth on for one test against an isolated credential file. Returns
    a client bound to the app. The session-wide conftest default of
    WEB_AUTH=none is restored on teardown by monkeypatch."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import app as app_mod
    from qobuz_librarian.web import auth as web_auth

    monkeypatch.setenv("WEB_AUTH", "")
    _run_web_executors_inline(monkeypatch)
    monkeypatch.setattr(cfg, "WEB_AUTH_FILE", tmp_path / "web_auth.json")
    if configure:
        assert web_auth.set_credentials("admin", "hunter2hunter2!")
    return _SameThreadASGIClient(app_mod.app)


def _client_ip(host, port, forwarded=""):
    from starlette.requests import Request

    from qobuz_librarian.web import auth as web_auth

    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return web_auth.client_ip(
        Request({"type": "http", "client": (host, port), "headers": headers}))


def test_a_forged_forwarded_header_does_not_get_a_visitor_named_as_the_proxy(
        monkeypatch, caplog):
    import logging

    from qobuz_librarian.web import auth as web_auth

    monkeypatch.setattr(web_auth, "_forwarding_peers", {})
    monkeypatch.setattr(web_auth, "_warned_proxy_peers", {})
    caplog.set_level(logging.WARNING, logger="qobuz_librarian")

    for _ in range(web_auth._PROXY_CLIENTS_TO_WARN + 1):
        _client_ip("192.168.1.50", 50000, "203.0.113.9")
    assert not caplog.records

    for host in range(web_auth._PROXY_CLIENTS_TO_WARN + 1):
        _client_ip("172.18.0.1", 40000, f"198.51.100.{host}")
    assert len(caplog.records) == 1
    assert "172.18.0.1" in caplog.records[0].getMessage()


def _mutation_paths(app):
    """Concrete inert paths for every unsafe route in the live route table."""
    return sorted(
        {
            route.path.replace("{job_id}", "guard-probe")
            for route in app.routes
            if "POST" in (getattr(route, "methods", None) or set())
        }
    )


def test_every_mutation_route_requires_csrf_and_a_session(monkeypatch, tmp_path):
    from qobuz_librarian.web import app as app_mod

    monkeypatch.setenv("WEB_AUTH", "none")
    with _SameThreadASGIClient(app_mod.app) as client:
        for path in _mutation_paths(app_mod.app):
            client.cookies.clear()
            response = client.post(path, follow_redirects=False)
            assert response.status_code == 403, path

    with _enable_auth(monkeypatch, tmp_path) as client:
        client.get("/login")
        csrf_token = client.cookies.get("ql_csrf")
        for path in _mutation_paths(app_mod.app):
            if path in {"/login", "/setup"}:
                continue
            response = client.post(
                path,
                headers={"X-CSRF-Token": csrf_token},
                follow_redirects=False,
            )
            assert response.status_code == 303, path
            assert response.headers["location"] == "/login", path


def test_failed_logins_lock_out_only_the_guesser(monkeypatch, tmp_path, caplog):
    """Wrong passwords lock the login out, even for the right password, on a
    page that still works once the wait is over, with one warning per try.
    Behind a proxy every visitor can arrive as the same address, so a
    stranger's guesses must not shut a signed-in browser out, and one device
    on the home IPv6 network cannot lock out another, while a /64 from the
    internet counts as one subscriber."""
    import logging

    from qobuz_librarian.web import auth as web_auth

    def reset_attempts():
        monkeypatch.setattr(web_auth, "_login_failures", {})
        monkeypatch.setattr(web_auth, "_user_failures", {})
        monkeypatch.setattr(web_auth, "_login_pending", {})
        monkeypatch.setattr(web_auth, "_user_paused_until", {})

    reset_attempts()
    caplog.set_level(logging.WARNING, logger="qobuz_librarian")

    with _enable_auth(monkeypatch, tmp_path) as c:
        c.get("/login")
        tok = c.cookies.get("ql_csrf")
        for _ in range(web_auth._LOGIN_MAX):
            r = c.post("/login",
                       data={"username": "admin", "password": "nope",
                             "_csrf_token": tok},
                       headers={"X-CSRF-Token": tok}, follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"].startswith("/login?error=")
            assert "ql_session" not in r.cookies
        # Still locked out afterwards.
        assert c.get("/", follow_redirects=False).status_code == 303

        r = c.post("/login",
                   data={"username": "admin", "password": "hunter2hunter2!",
                         "_csrf_token": tok},
                   headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert r.status_code == 429
        assert 'action="/login"' in r.text
        assert "ql_session" not in r.cookies

    warnings = [record.getMessage() for record in caplog.records
                if record.levelno == logging.WARNING]
    assert len(warnings) == web_auth._LOGIN_MAX + 1
    assert all("127.0.0.1" in message for message in warnings)

    reset_attempts()
    with _enable_auth(monkeypatch, tmp_path) as c:
        c.get("/login")
        tok = c.cookies.get("ql_csrf")
        signed_in = c.post("/login",
                           data={"username": "admin",
                                 "password": "hunter2hunter2!",
                                 "_csrf_token": tok},
                           headers={"X-CSRF-Token": tok},
                           follow_redirects=False)
        assert signed_in.status_code == 303

        for _ in range(web_auth._LOGIN_MAX):
            assert web_auth.begin_login_attempt("testclient", "admin")
            web_auth.finish_login_attempt("testclient", "admin", success=False)
        assert not web_auth.begin_login_attempt("testclient", "admin")

        c.get("/login")
        tok = c.cookies.get("ql_csrf")
        r = c.post("/login",
                   data={"username": "admin", "password": "hunter2hunter2!",
                         "_csrf_token": tok},
                   headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert r.status_code == 303
        assert c.get("/", follow_redirects=False).status_code == 200

    reset_attempts()
    guest = _client_ip("2a02:8070:1:2::bad", 51000)
    for _ in range(web_auth._LOGIN_MAX):
        assert web_auth.begin_login_attempt(guest, "admin")
        web_auth.finish_login_attempt(guest, "admin", success=False)
    owner = _client_ip("2a02:8070:1:2:aaaa:bbbb:cccc:dddd", 51001)
    assert web_auth.begin_login_attempt(owner, "admin")
    web_auth.cancel_login_attempt(owner, "admin")

    # From the internet, through a trusted proxy (uvicorn has already put the
    # forwarded client in place, with no port), a /64 is one subscriber, so
    # moving to a fresh address in it buys no more guesses.
    for host in range(1, web_auth._LOGIN_MAX + 1):
        hop = f"2a02:8070:9:9::{host}"
        hop_ip = _client_ip(hop, 0, hop)
        assert web_auth.begin_login_attempt(hop_ip, "someone")
        web_auth.finish_login_attempt(hop_ip, "someone", success=False)
    fresh = "2a02:8070:9:9::99"
    assert not web_auth.begin_login_attempt(_client_ip(fresh, 0, fresh), "someone")


def test_login_next_and_the_host_header_cannot_lead_past_sign_in(
        client, monkeypatch, tmp_path):
    """The next field is attacker-writable (it rides links and the login
    form), so anything that could land off-site or loop falls back to "/". A
    name rebound to this server reaches nothing, even with sign-in off.
    CVE-2026-48710: Starlette rebuilds request.url.path from the client Host
    header, so a host like "example.com/login?x=" could make the auth
    middleware read the path as "/login" and wave a protected route through
    with no session."""
    from qobuz_librarian.web import auth as web_auth

    for bad in ("//evil.example", "/\\evil.example"):
        assert web_auth.safe_next_path(bad) == "", bad
    assert web_auth.safe_next_path("/queue") == "/queue"
    assert web_auth.safe_next_path("/jobs/abc?x=1") == "/jobs/abc?x=1"

    assert client.get("/queue", headers={"Host": "rebind.attacker.example"}).status_code == 400

    with _enable_auth(monkeypatch, tmp_path) as c:
        c.get("/login")
        tok = c.cookies.get("ql_csrf")
        r = c.post("/login",
                   data={"username": "admin", "password": "hunter2hunter2!",
                         "_csrf_token": tok, "next": "//evil.example"},
                   headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"

    with _enable_auth(monkeypatch, tmp_path) as c:
        bad = {"host": "example.com/login?x="}
        # Page route: redirected to login, never served.
        r = c.get("/settings", headers=bad, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/login")
        # JSON route: 401, not a 200 leaking state.
        r = c.get("/api/jobs", headers=bad, follow_redirects=False)
        assert r.status_code == 401
        # Write route is unreachable too (never a 200).
        r = c.post("/queue/cancel-pending", headers=bad,
                   follow_redirects=False)
        assert r.status_code != 200


def test_saved_login_survives_restarts_and_a_new_password_retires_old_sessions(
        tmp_path, monkeypatch):
    """A stale session file must not revive prior-password access on restart,
    and signing one browser out leaves another signed in. A password set in
    Settings outlives the environment's on restart and survives unreadable
    storage, while a new password in the environment still wins, even over a
    broken saved login."""
    import errno

    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import auth as web_auth

    monkeypatch.setattr(cfg, "WEB_AUTH_FILE", tmp_path / "rotated.json")
    monkeypatch.setattr(web_auth, "_SESSIONS_FILE", tmp_path / "sessions.json")
    with web_auth._sessions_lock:
        original_sessions = dict(web_auth._sessions)
        web_auth._sessions = {}
    with monkeypatch.context() as patch:
        try:
            assert web_auth.set_credentials("admin", "first secure password")
            generation = web_auth.verify_login_generation("admin", "first secure password")
            assert generation
            old_token = web_auth.mint_session()
            assert web_auth.verify_session(old_token)
            # Signing one browser out leaves another signed in.
            one, other = web_auth.mint_session(), web_auth.mint_session()
            web_auth.revoke_session(one)
            assert not web_auth.verify_session(one) and web_auth.verify_session(other)

            # Credential publication succeeds, but invalidating the durable session
            # file does not. Simulate process reconstruction from that stale file.
            patch.setattr(web_auth, "_save_sessions_locked", lambda: False)
            assert web_auth.set_credentials("admin", "second secure password")
            with pytest.raises(web_auth.CredentialsChanged):
                web_auth.mint_session(expected_generation=generation)
            with web_auth._sessions_lock:
                web_auth._sessions = web_auth._load_sessions()

            assert web_auth.verify_session(old_token) is False
        finally:
            with web_auth._sessions_lock:
                web_auth._sessions = original_sessions

    monkeypatch.setattr(cfg, "WEB_AUTH_FILE", tmp_path / "auth.json")
    monkeypatch.delenv("WEB_AUTH", raising=False)
    monkeypatch.setenv("WEB_AUTH_USER", "admin")
    monkeypatch.setenv("WEB_AUTH_PASSWORD", "ember orbit atlas")
    monkeypatch.delenv("WEB_AUTH_PASSWORD_FILE", raising=False)
    assert web_auth.apply_env_credentials() == "applied"

    web_auth.set_credentials("admin", "quiet harbour lantern",
                             env_password_hash=web_auth.env_override_hash())
    assert web_auth.apply_env_credentials() == "kept"
    assert web_auth.verify_login("admin", "quiet harbour lantern")
    assert not web_auth.verify_login("admin", "ember orbit atlas")

    saved = cfg.WEB_AUTH_FILE.read_bytes()
    read_text, stat = Path.read_text, Path.stat

    def unavailable_read(path, *args, **kwargs):
        if path == cfg.WEB_AUTH_FILE:
            raise OSError(errno.EIO, "storage unavailable")
        return read_text(path, *args, **kwargs)

    def unavailable_stat(path, *args, **kwargs):
        if path == cfg.WEB_AUTH_FILE:
            raise OSError(errno.EIO, "storage unavailable")
        return stat(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(web_auth, "_cred_cache", None)
        fault.setattr(web_auth, "_cred_cache_path", None)
        fault.setattr(Path, "read_text", unavailable_read)
        fault.setattr(Path, "stat", unavailable_stat)
        assert not web_auth.setup_credentials("replacement", "another secure password")
        assert web_auth.apply_env_credentials() == "failed"
    assert cfg.WEB_AUTH_FILE.read_bytes() == saved
    assert web_auth.verify_login("admin", "quiet harbour lantern")

    # Editing the environment is still the way back in after a forgotten
    # password, so a new value there has to win.
    monkeypatch.setenv("WEB_AUTH_PASSWORD", "distant meadow signal")
    assert web_auth.apply_env_credentials() == "applied"
    assert web_auth.verify_login("admin", "distant meadow signal")

    # Including when the saved login itself is what broke.
    cfg.WEB_AUTH_FILE.write_text("{", encoding="utf-8")
    web_auth._cred_cache = None
    assert web_auth.apply_env_credentials() == "applied"
    assert web_auth.verify_login("admin", "distant meadow signal")
    assert (tmp_path / "auth.json.corrupt").read_text(encoding="utf-8") == "{"


def test_restore_and_startup_put_backups_home_but_never_by_a_path(
        client, tmp_path, monkeypatch):
    """The Restore form posts a bare directory name; anything path-shaped is
    a probe, not a backup the diagnostics list rendered, so it must not
    resolve outside the backup dir or restore anything. Restore moves the
    files home and clears the job's recovery, and Discard keeps a backup
    whose files differ from the library's copy. A hard stop between an
    upgrade's or repair's backup and its import left the folder missing,
    with nothing but a Diagnostics row to say so; taking the lock puts it
    back once staging is free."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import backup as backup_mod
    from qobuz_librarian.web import job_persistence, lifespan, runtime

    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "probe" / "backups")
    (tmp_path / "probe" / "backups").mkdir(parents=True)
    r = client.post("/backups/restore", data={"backup": "../../etc"})
    assert r.status_code == 200
    assert 'data-flash-kind="error"' in r.text

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    root = tmp_path / "restore"
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", root / "backups")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", root / "music")
    origin = root / "music" / "Artist" / "Album (2020)"
    origin.mkdir(parents=True)
    (origin / "01 - Song.flac").write_bytes(b"data")
    carried = backup_mod.backup_album_dir(origin)
    assert carried is not None and carried.complete is True
    job = jm.Job(title="Repair needing recovery")
    job.execute_kind = "repair"
    job.status = jm.JobStatus.FAILED
    job.finished_at = time.time()
    job.attention = "recovery"
    job.recoveries = [_repair_recovery_record(carried.path, carried.receipt)]
    jm.registry.add(job)
    try:
        r = client.post("/backups/restore", data={"backup": carried.name})
        assert r.status_code == 200
        assert (origin / "01 - Song.flac").read_bytes() == b"data"
        assert not carried.exists()
        assert job.recoveries == []
        assert job.attention == ""
        assert job_persistence.load_one(job.id)["recoveries"] == []

        kept = backup_mod.backup_album_dir(origin)
        origin.mkdir(parents=True)
        (origin / "01 - Song.flac").write_bytes(b"changed")
        assert client.post("/backups/discard", data={"backup": kept.name}).status_code == 200
        assert (kept.path / "01 - Song.flac").read_bytes() == b"data"
    finally:
        _remove_job(job)

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()
    root = tmp_path / "hard-stop"
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", root / "backups")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", root / "music")
    origin = root / "music" / "Artist" / "Album (2020)"
    origin.mkdir(parents=True)
    (origin / "01 - Song.flac").write_bytes(b"data")
    carried = backup_mod.backup_album_dir(origin)
    other = root / "music" / "Artist" / "Other (2021)"
    other.mkdir()
    (other / "01 - Song.flac").write_bytes(b"old")
    kept = backup_mod.backup_album_dir(other)
    other.mkdir()
    (other / "01 - Song.flac").write_bytes(b"new")
    job = jm.Job(title="Repair needing recovery")
    job.execute_kind = "repair"
    job.status = jm.JobStatus.FAILED
    job.finished_at = time.time()
    job.attention = "recovery"
    job.recoveries = [_repair_recovery_record(carried.path, carried.receipt)]
    jm.registry.add(job)
    try:
        busy = jm.staging_lock()
        busy.acquire()
        try:
            lifespan._put_back_albums_left_in_backups()
        finally:
            busy.release()
        assert not origin.exists() and lifespan._put_back_pending
        lifespan._put_back_albums_left_in_backups()
        assert not lifespan._put_back_pending
        assert (origin / "01 - Song.flac").read_bytes() == b"data"
        assert not carried.exists()
        assert job_persistence.load_one(job.id)["recoveries"] == []
        assert runtime.take_restored_albums() == [origin]
        assert (kept.path / "01 - Song.flac").read_bytes() == b"old"
        assert (other / "01 - Song.flac").read_bytes() == b"new"
    finally:
        _remove_job(job)

    # A hard stop can also land while the album is half way into its
    # backup: renamed but not yet sealed, or copied to another filesystem
    # with its folder part removed. Both come back on the next start, and
    # the reviews of the one rebuilt from the copy are sealed again.
    moved = root / "music" / "Artist" / "Moved (2022)"
    copied = root / "music" / "Artist" / "Copied (2023)"
    for album in (moved, copied):
        album.mkdir()
        (album / "01 - Song.flac").write_bytes(b"one")
        (album / "02 - Song.flac").write_bytes(b"two")
    script = """
import os, sys
from pathlib import Path
from qobuz_librarian import config as cfg
from qobuz_librarian.library import backup as backup_mod
music, backups, album = sys.argv[1:]
cfg.MUSIC_ROOT = Path(music)
cfg.UPGRADE_BACKUP_DIR = Path(backups)
if album.endswith("Moved (2022)"):
    backup_mod._write_backup_origin_durable = lambda *_args: os._exit(91)
elif album.endswith("Renamed (2024)"):
    backup_mod._remove_put_back_sidecars = lambda *_args: os._exit(91)
    path = next(path for path, origin in backup_mod.albums_left_in_backups()
                if origin == Path(album))
    backup_mod.restore_upgrade_backup(
        backup_mod.load_backup_result(path), Path(album))
    os._exit(92)
else:
    backup_mod._same_filesystem = lambda *_args: False
    unlink = backup_mod._unlink_exact_at

    def unlink_then_stop(*args):
        unlink(*args)
        os._exit(91)

    backup_mod._unlink_exact_at = unlink_then_stop
backup_mod.backup_album_dir(Path(album))
os._exit(92)
"""
    child_home = str(tmp_path / "child")
    for album in (moved, copied):
        stopped = subprocess.run(
            [sys.executable, "-c", script, str(root / "music"),
             str(root / "backups"), str(album)],
            env={**os.environ, "DATA_DIR": child_home, "HOME": child_home})
        assert stopped.returncode == 91
        assert not album.exists()
    resealed = []
    monkeypatch.setattr(backup_mod, "on_restored",
                        lambda receipt: resealed.append(receipt["origin"]))
    lifespan._put_back_albums_left_in_backups()
    assert runtime.take_restored_albums() == [moved, copied]
    assert resealed == [str(copied)]
    for album in (moved, copied):
        assert sorted(p.name for p in album.iterdir()) == [
            "01 - Song.flac", "02 - Song.flac"]
        assert (album / "02 - Song.flac").read_bytes() == b"two"
    assert sorted(p.name for p in (root / "backups").iterdir()) == [
        kept.name]
    assert sorted(p.name for p in (root / "music" / "Artist").iterdir()) == [
        "Album (2020)", "Copied (2023)", "Moved (2022)", "Other (2021)"]

    # A hard stop after a restore renamed an album home but before it took
    # the backup's own files out of it; the next start removes them.
    renamed = root / "music" / "Artist" / "Renamed (2024)"
    renamed.mkdir()
    (renamed / "01 - Song.flac").write_bytes(b"one")
    assert backup_mod.backup_album_dir(renamed).complete
    stopped = subprocess.run(
        [sys.executable, "-c", script, str(root / "music"),
         str(root / "backups"), str(renamed)],
        env={**os.environ, "DATA_DIR": child_home, "HOME": child_home})
    assert stopped.returncode == 91
    assert (renamed / backup_mod._RECEIPT_SIDECAR).exists()
    lifespan._put_back_albums_left_in_backups()
    assert sorted(p.name for p in renamed.iterdir()) == ["01 - Song.flac"]
    assert sorted(p.name for p in (root / "backups").iterdir()) == [
        kept.name]


def test_refresh_folds_into_parked_library_review(monkeypatch):
    from qobuz_librarian.web import job_persistence, review_badges, scans

    badge_calls = []
    monkeypatch.setattr(
        review_badges,
        "mark_ready",
        lambda surface: badge_calls.append(surface),
    )
    monkeypatch.setattr(job_persistence, "_write_snapshot", lambda *_a: True)

    parked = jm.Job(title="Library scan")
    parked.execute_kind = "library"
    parked.add_candidate(
        kind="album",
        title="Dummy",
        artist="Portishead",
        detail="1994 · 16-bit/44.1 kHz · 11 tracks",
        payload={"album_id": "al1", "is_new": True},
        selected=False,
    )
    parked.add_candidate(
        kind="album",
        title="Third",
        artist="Portishead",
        detail="2008 · 24-bit/44.1 kHz · 10 tracks",
        payload={"album_id": "al2"},
        selected=False,
    )
    parked.status = jm.JobStatus.AWAITING_REVIEW
    parked.set_selected(parked.candidates[0]["cid"], True)
    jm.registry.add(parked)

    scan = jm.Job(title="Library scan")
    scan.execute_kind = "library"
    scan.execute_args = {"_library_review_generation": 123.0}
    scan.status = jm.JobStatus.SCANNING
    scan.add_candidate(
        kind="album",
        title="Dummy",
        artist="Portishead",
        detail="1994 · 16-bit/44.1 kHz · 11 tracks",
        payload={"album_id": "al1"},
        selected=False,
    )
    # Third came back from Dismissed after the last scan, which therefore
    # did not record it.
    scan.add_candidate(
        kind="album",
        title="Third",
        artist="Portishead",
        detail="2008 · 24-bit/44.1 kHz · 10 tracks",
        payload={"album_id": "al2", "is_new": True},
        selected=False,
    )
    scan.add_candidate(
        kind="album",
        title="Roseland NYC Live",
        artist="Portishead",
        detail="1998 · 16-bit/44.1 kHz · 11 tracks",
        payload={"album_id": "al3", "release_type": "live"},
        selected=False,
    )
    jm.registry.add(scan)
    changed_scan = rescan = None
    try:
        scans._fold_into_parked_library_review(scan)

        # Dummy only lost its "new" badge, Third was already listed and the
        # live album sits behind its switch, so the review lists nothing new.
        assert [bool(c["payload"].get("is_new")) for c in parked.candidates] == [
            False, False, False]
        assert badge_calls == []
        assert scan.status == jm.JobStatus.DONE
        assert scan.candidates == []
        ids = [c["payload"]["album_id"] for c in parked.candidates]
        assert ids == ["al1", "al2", "al3"]
        ticked = [c["payload"]["album_id"] for c in parked.candidates if c.get("selected")]
        assert ticked == ["al1"]
        assert parked.execute_args["_library_review_generation"] == 123.0
        assert parked.status == jm.JobStatus.AWAITING_REVIEW
        library_reviews = [j for j in jm.registry.awaiting_review() if j.execute_kind == "library"]
        assert library_reviews == [parked]

        parked.add_candidate(
            kind="album",
            title="Changing Album",
            artist="Portishead",
            detail="gap-fill: 1 of 10 tracks missing",
            payload={
                "album_id": "gap1",
                "gap_fill": 1,
                "refresh_generation": "old",
            },
            selected=True,
        )
        old_gap = parked.candidates[-1]
        old_identity = old_gap["cid"], old_gap["seq"]
        badge_calls.clear()
        changed_scan = jm.Job(title="Library scan")
        changed_scan.execute_kind = "library"
        changed_scan.execute_args = {"_library_review_generation": 124.0}
        changed_scan.status = jm.JobStatus.SCANNING
        changed_scan.add_candidate(
            kind="album",
            title="Changing Album",
            artist="Portishead",
            detail="gap-fill: 4 of 10 tracks missing",
            payload={
                "album_id": "gap1",
                "gap_fill": 4,
                "refresh_generation": "fresh",
            },
            selected=False,
        )
        jm.registry.add(changed_scan)

        scans._fold_into_parked_library_review(changed_scan)

        fresh_gap = next(c for c in parked.candidates if c["payload"].get("album_id") == "gap1")
        assert (fresh_gap["cid"], fresh_gap["seq"]) == old_identity
        assert fresh_gap["selected"] is True
        assert fresh_gap["detail"] == "gap-fill: 4 of 10 tracks missing"
        assert fresh_gap["payload"]["gap_fill"] == 4
        assert fresh_gap["payload"]["refresh_generation"] == "fresh"
        assert parked.execute_args["_library_review_generation"] == 124.0
        assert badge_calls == ["library"]

        # A finished scan that derived Portishead again settles the unticked
        # rows it no longer offers there and the ticked ones it found on disk.
        # It keeps the other ticked ones, sealed with the folder as it read it
        # so the pick still downloads; another artist's rows are left alone.
        parked.add_candidate(
            kind="album",
            title="Mezzanine",
            artist="Massive Attack",
            detail="1998 · 16-bit/44.1 kHz · 11 tracks",
            payload={"album_id": "ma1"},
            selected=False,
        )
        parked.add_candidate(
            kind="album",
            title="Glory Times",
            artist="Portishead",
            detail="1995 · 16-bit/44.1 kHz · 4 tracks",
            payload={"album_id": "al4", "_artist_dir_path": "/music/Portishead",
                     "_premise": {"seal": "old"}},
            selected=True,
        )
        parked.add_candidate(
            kind="album",
            title="Roseland NYC Live (Remastered)",
            artist="Portishead",
            detail="1998 · 24-bit/96 kHz · 11 tracks",
            payload={"album_id": "al5"},
            selected=True,
        )
        rescan = jm.Job(title="Library scan")
        rescan.execute_kind = "library"
        rescan.status = jm.JobStatus.SCANNING
        rescan.add_candidate(
            kind="album",
            title="Dummy",
            artist="Portishead",
            detail="1994 · 16-bit/44.1 kHz · 11 tracks",
            payload={"album_id": "al1", "_artist_dir_path": "/music/Portishead",
                     "_premise": {"seal": "fresh"}},
            selected=False,
        )
        rescan.scan_coverage = (frozenset({"Portishead"}), False,
                                frozenset({"al5"}))
        jm.registry.add(rescan)

        scans._fold_into_parked_library_review(rescan)

        assert [(c["payload"]["album_id"], c["selected"])
                for c in parked.candidates] == [
            ("al1", True), ("gap1", True), ("ma1", False), ("al4", True)]
        assert parked.candidates[-1]["payload"]["_premise"] == {"seal": "fresh"}
        assert rescan.log_lines[:-1] == parked.log_lines[-3:]
    finally:
        _remove_job(parked)
        _remove_job(scan)
        if changed_scan is not None:
            _remove_job(changed_scan)
        if rescan is not None:
            _remove_job(rescan)


def _restore_stack(monkeypatch, tmp_path, albums):
    """An empty library and a catalogue that answers by album id."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import collection_restore

    music = tmp_path / "music"
    music.mkdir()
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "COLLECTION_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(collection_restore.scanner, "clear_scan_caches",
                        lambda: None)
    monkeypatch.setattr(collection_restore, "get_album",
                        lambda album_id, _t: albums[str(album_id)])
    monkeypatch.setattr(collection_restore, "find_qobuz_track_by_isrc",
                        lambda *_a: None)
    monkeypatch.setattr(collection_restore, "search_albums",
                        lambda *_a, **_k: [])


def _backup_file(album_count=1, padding=0):
    import json

    from qobuz_librarian.library import collection_snapshot

    albums = [{"name": f"Album {i}", "qobuz_album_id": f"a{i}", "tracks": []}
              for i in range(album_count)]
    document = {"format": collection_snapshot.FORMAT,
                "version": collection_snapshot.VERSION,
                "counts": {"artists": 1, "albums": album_count, "tracks": 0},
                "artists": [{"name": "Noname", "albums": albums}]}
    if padding:
        document["music_root"] = "x" * padding
    return json.dumps(document).encode("utf-8")


def test_uploading_a_backup_parks_one_restore_review(client, monkeypatch,
                                                     tmp_path):
    album = {"id": "a0", "title": "Album 0", "tracks_count": 8,
             "maximum_bit_depth": 16, "maximum_sampling_rate": 44.1,
             "artist": {"name": "Noname"},
             "tracks": {"items": [{"id": "t1"}]}}
    _restore_stack(monkeypatch, tmp_path, {"a0": album})
    jm.start_worker()

    # Deliberately past the 1 MB form cap: a real backup is several MB, and the
    # upload route is the one path allowed past it.
    response = client.post(
        "/collection/restore",
        files={"backup": ("collection.json", _backup_file(padding=2_000_000),
                          "application/json")},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    job_id = response.headers["HX-Redirect"].rsplit("/", 1)[-1]
    job = jm.registry.get(job_id)
    try:
        assert _wait_for(
            lambda: job.status == jm.JobStatus.AWAITING_REVIEW)
        assert [c["payload"]["album_id"] for c in job.candidates] == ["a0"]
        assert job.candidates[0]["selected"] is True
    finally:
        _remove_job(job)
