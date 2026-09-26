"""Tests for the web UI: background job system (jobs.py) and HTTP routes (app.py).

Trimmed to a maintainable representative set: data-safety paths (restore,
hide/restore round-trip, migration move-vs-copy, persist-without-tearing),
auth/session/CSRF, the run-lock destructive-route guard, settings save/load,
one search + one approve endpoint, and a few genuinely tricky bits of logic.
"""

import asyncio
import copy
import json
import sqlite3
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


def test_staging_lock_serialises_lane_album_work():
    """Both lanes interleave at the album level: only one rip+import at a
    time, even with two workers running. Guards against /staging races and
    beets' SQLite write lock."""
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


def test_scan_job_parks_for_review_then_executes():
    jm.start_worker()
    executed = {}

    def scan(j):
        j.add_candidate("album", "Album A", "Artist", payload={"id": 1})
        j.add_candidate("album", "Album B", "Artist", payload={"id": 2})

    def execute(j, chosen):
        executed["ids"] = [c["payload"]["id"] for c in chosen]

    job = jm.Job(title="scan")
    jm.submit_scan(job, scan, execute)
    assert _wait_for(lambda: job.status == jm.JobStatus.AWAITING_REVIEW)
    assert len(job.candidates) == 2
    assert jm.approve(job, ["c1"])
    assert _wait_for(lambda: job.status == jm.JobStatus.DONE)
    assert executed["ids"] == [2]


def test_post_job_hook_delivers_json_to_an_inert_command(tmp_path, monkeypatch):
    sink = tmp_path / "hook.json"
    payload = {
        "id": "local-hook-check",
        "status": "done",
        "title": "Album",
    }
    monkeypatch.setenv("HOOK_SINK", str(sink))
    monkeypatch.setenv("POST_JOB_HOOK", 'tee "$HOOK_SINK"')

    jm._run_post_job_hook(payload)

    assert sink.read_text() == (
        '{"id": "local-hook-check", "status": "done", "title": "Album"}'
    )


def test_per_artist_rescan_supersedes_only_that_artists_parked_review(
        monkeypatch):
    # Two artists each have a scan parked for review.
    from qobuz_librarian.web import job_persistence, runtime, scans

    monkeypatch.setattr(job_persistence, "persist", lambda _job: True)
    monkeypatch.setattr(
        job_persistence, "ready_for_admission", lambda: True)

    class Authority:
        @staticmethod
        def intact():
            return True

    monkeypatch.setattr(runtime, "_RUN_LOCK_HANDLE", Authority())

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


def test_download_dedup_respects_new_edition_and_single_track_intent():
    # Folding a /download onto an in-flight job must respect intent, not just the
    # album id: "get this edition too" is a deliberate extra copy and a one-track
    # grab is its own thing, and neither should be swallowed by an unrelated job for
    # the same album, and a full-album download must not fold onto a one-track grab.
    from qobuz_librarian.web import download_admission

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


def test_queued_download_rechecks_the_music_root_before_it_runs(monkeypatch):
    from qobuz_librarian.library import collection_snapshot
    from qobuz_librarian.web import flows, job_runs

    monkeypatch.setattr(
        collection_snapshot,
        "music_root_write_state",
        lambda: ("recorded_empty", 7),
    )
    monkeypatch.setattr(
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


def test_cancel_while_queued_finalizes_and_worker_skips_it():
    # A scan queued behind a busy lane, cancelled before it starts, is finalized
    # to CANCELED at once (it doesn't linger as "Queued" until the job ahead of
    # it finishes), and when the lane frees the worker drops it rather than
    # running it.
    import threading

    jm.start_worker()
    release = threading.Event()
    holding = threading.Event()

    holder = jm.Job(title="lane holder")
    holder.kind = "scan"

    def _hold(j):
        holding.set()
        release.wait(timeout=5)

    jm.registry.add(holder)
    jm._scan_queue.put((holder, _hold))
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


def test_album_search_drops_artist_only_matches(client, monkeypatch):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as catalog_mod
    from qobuz_librarian.web import qobuz_access

    monkeypatch.setattr(qobuz_access, "_get_token", lambda: "tok")
    monkeypatch.setattr(catalog_mod, "find_album_dir_filesystem", lambda _a: None)
    monkeypatch.setattr(search_mod, "qobuz_get", lambda *_a, **_kw: {
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


def test_new_edition_download_rechecks_exact_ownership(
        client, monkeypatch, tmp_path):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as catalog_mod
    from qobuz_librarian.web import job_runs, qobuz_access

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
        lambda _album: folder[0],
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


# The Doors put out a 25-track "50th Anniversary Deluxe Edition" and two plain
# 11-track pressings of Waiting for the Sun, none of which carries a version
# field. That is the shape that breaks a grouped row.


def test_settings_save_defers_apply_when_job_is_active(tmp_path, monkeypatch):
    """An in-flight job must not see cfg.* flip mid-run."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(cfg, "DOWNSAMPLE_HIRES_ENABLED", False)
    monkeypatch.setattr(ss, "_any_active_job", lambda: True)
    monkeypatch.setattr(ss, "_pending_apply", None)

    ok, _ = ss.save({"DOWNSAMPLE_HIRES_ENABLED": True})
    assert ok is True
    assert (tmp_path / "s.json").exists()
    assert cfg.DOWNSAMPLE_HIRES_ENABLED is False  # not yet applied

    ss.drain_pending()
    assert cfg.DOWNSAMPLE_HIRES_ENABLED is True
    ss.drain_pending()
    assert cfg.DOWNSAMPLE_HIRES_ENABLED is True  # idempotent


def test_clearing_a_field_goes_back_to_the_compose_value(tmp_path, monkeypatch):
    """Emptying a text field means "use the environment again", not "save a
    blank". Saving the blank pinned it, so the Compose value was gone for good
    and the settings file had to be edited by hand to get it back."""
    import json

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


def test_settings_save_keeps_custom_timer_values(client, tmp_path, monkeypatch):
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
    monkeypatch.setattr(cfg, "NEW_RELEASE_CHECK_INTERVAL", 3600)
    monkeypatch.setattr(cfg, "ARTIST_CATALOG_CACHE_TTL", 0)
    monkeypatch.setattr(cfg, "LYRICS_ENABLED", True)

    page = client.get("/settings")
    assert page.status_code == 200
    form = next(data for data in Forms(page.text).forms if "form_complete" in data)
    del form["LYRICS_ENABLED"]
    response = client.post("/settings/behavior", data=form, follow_redirects=False)

    assert response.status_code == 303
    assert cfg.LYRICS_ENABLED is False
    assert cfg.NEW_RELEASE_CHECK_INTERVAL == 3600
    assert cfg.ARTIST_CATALOG_CACHE_TTL == 0
    assert json.loads(store.read_text()) == {"LYRICS_ENABLED": False}
    # A field saved before stays saved when the form posts it unchanged.
    client.post("/settings/behavior", data=form, follow_redirects=False)
    assert json.loads(store.read_text()) == {"LYRICS_ENABLED": False}


def test_concurrent_settings_saves_merge_without_losing_either_change(tmp_path, monkeypatch):
    import json
    import threading

    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

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


# ── run-lock busy → destructive routes 503, read-only stay open ───────


def test_lock_busy_refuses_destructive_routes(monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import qobuz_access, runtime

    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", True, raising=False)
    monkeypatch.setattr(qobuz_access, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(qobuz_access, "_TOKEN_VALID", True)
    _run_web_executors_inline(monkeypatch)
    with _SameThreadASGIClient(webapp.app) as c:
        c.get("/queue")
        token = c.cookies.get("ql_csrf")
        c.headers.update({"X-CSRF-Token": token})
        monkeypatch.setattr(runtime, "_LOCK_BUSY_PID", 4321)

        dash = c.get("/")
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
            r = c.post(path, data=data, follow_redirects=False)
            assert r.status_code == 503, f"{path} should 503 when lock busy"
            assert "pid 4321" not in r.text
            assert "run-lock" not in r.text


@pytest.mark.parametrize(
    ("execute_kind", "path"),
    [
        ("library", "/library"),
        ("upgrade", None),
    ],
)
def test_saved_remote_reviews_remain_visible_without_qobuz(
        client, monkeypatch, execute_kind, path):
    from qobuz_librarian.web import qobuz_access

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
    monkeypatch.setattr(qobuz_access, "_read_creds", lambda: {})
    monkeypatch.setattr(qobuz_access, "_qobuz_ready", lambda: False)
    destination = path or f"/jobs/{job.id}"
    try:
        response = client.get(destination)

        assert response.status_code == 200
        assert "Saved Album" in response.text
        assert 'data-review-blocked="1"' in response.text
        assert 'id="review-submit"' in response.text
    finally:
        _remove_job(job)


def test_upgrade_saved_review_respects_hidden_candidates(
        client, monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden
    from qobuz_librarian.web import job_persistence, qobuz_access, saved_reviews
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    monkeypatch.setattr(job_persistence, "_persist_locked", lambda _job: True)
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

    r = client.get("/upgrade")
    assert r.status_code == 200
    assert saved_reviews._upgrade_state_summary()["count"] == 1

    second = client.post("/upgrade/review", follow_redirects=False)
    assert second.headers["location"] == first.headers["location"]
    assert len([
        j for j in job_mgr.registry.awaiting_review()
        if j.execute_kind == "upgrade"
    ]) == 1


@pytest.mark.parametrize("execute_kind", ["library", "upgrade"])
def test_qobuz_approval_failure_preserves_the_exact_review(
        client, monkeypatch, execute_kind):
    from qobuz_librarian.api.auth import AuthLost
    from qobuz_librarian.web import qobuz_access, saved_reviews

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


def test_auth_failure_before_any_import_reparks_the_review():
    """Qobuz dying on the FIRST album of an approved run must not consume the
    review: the picks go back to awaiting-review instead of a failed job."""
    from qobuz_librarian.api.auth import AuthLost

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


def test_a_split_review_that_cannot_be_put_back_fails_without_hanging(
    monkeypatch,
):
    """The failed save is logged while the job's lock is held, and the job's
    own log handler takes that lock again."""
    from qobuz_librarian.api.auth import AuthLost
    from qobuz_librarian.web import job_persistence

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

    def _dies(j, chosen):
        monkeypatch.setattr(job_persistence, "_get_conn", lambda: broken)
        raise AuthLost("token rejected")

    job._execute_fn = _dies
    jm.registry.add(job)
    try:
        jm.start_worker()
        assert jm.approve(job, None, split_review=lambda _job: remnant) is True
        assert _wait_for(lambda: job.status == jm.JobStatus.FAILED)
    finally:
        _remove_job(job)
        _remove_job(remnant)


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
    _remove_job(new_job)


def test_giving_up_a_download_that_staged_nothing_lifts_the_pause(
    client, monkeypatch, tmp_path,
):
    """A rip that fails before writing anything leaves its saved queue entry
    waiting on Retry, which pauses every download and scan and fails the same
    way each time. Give up has to clear it."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian import run_lock
    from qobuz_librarian.queue import journal
    from qobuz_librarian.queue.builder import _build_queue_item
    from qobuz_librarian.web import job_persistence, queue_recovery, runtime

    monkeypatch.setattr(cfg, "LOCK_FILE", tmp_path / "run.lock")
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

    def _record(lease):
        result = queue_recovery._recover_startup_queue(lease)
        queue_recovery._STARTUP_RECOVERY_RESULT = result
        return result

    authority = run_lock.acquire()
    monkeypatch.setattr(runtime, "_RUN_LOCK_HANDLE", authority)
    monkeypatch.setattr(queue_recovery, "_record_startup_recovery", _record)
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


def test_library_approve_scoped_to_tab_splits_off_other_tab(client, monkeypatch):
    """Downloading from one tab must consume only that tab: the other tab's
    candidates (and their saved ticks) split into their own parked review
    instead of dying with the executing job."""
    from qobuz_librarian.web import qobuz_access
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
        r = client.post(f"/jobs/{job.id}/approve", data={"tab": "missing"},
                        follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/library?approved=1"
        # The approved job carries only the active tab's candidates.
        assert [c["title"] for c in job.candidates] == ["Third"]
        assert job.status != jm.JobStatus.AWAITING_REVIEW
        # The gap candidates live on in a new parked review, ticks intact.
        split = next(j for j in jm.registry.all()
                     if j is not job and j.execute_kind == "library"
                     and j.status == jm.JobStatus.AWAITING_REVIEW)
        titles = {c["title"]: c["selected"] for c in split.candidates}
        assert titles == {"Dummy": True, "Untrue": False}
        # History lists a parked review by its summary, and the split used to
        # carry none, so the row arrived with nothing in it.
        assert split.summary
        assert split._execute_fn is not None
    finally:
        _remove_job(job)
        if split is not None:
            _remove_job(split)


def test_select_all_scoped_to_the_active_filter(client):
    """With a filter showing 3 rows, Select all must not silently flip
    the other thousand, and Deselect must scope the same way so a filtered
    select-all can be undone filtered."""
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


def test_library_dismiss_rest_hides_everything_unselected(client, monkeypatch, tmp_path):
    from qobuz_librarian.library import hidden
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", tmp_path / "h.json")
    monkeypatch.setattr(job_persistence, "_persist_locked", lambda _job: True)

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

        r = client.post(f"/jobs/{job.id}/dismiss-rest")
        assert r.status_code == 200
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


def test_a_finished_download_is_not_failed_by_another_items_recovery(
        monkeypatch):
    """Startup recovery is process-wide. A download whose own completion is
    durably acknowledged was written up as "Failed / Recovery attention"
    because some other item's recovery was outstanding; one blocked in the
    terminal owns no web job id at all and relabelled every finished download
    in History at once.
    """
    from qobuz_librarian.web import job_persistence

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


def test_a_restart_requeues_waiting_downloads_but_not_the_started_one(
        monkeypatch):
    # Hundreds of queued albums came back as failed rows to retry one by
    # one. Only the download that had started may be failed: replaying it
    # could repeat work that already touched the library.
    from qobuz_librarian.web import job_persistence, job_runs

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()
    started = jm.Job(title="First", album_id="1", created_at=100.0,
                     status=jm.JobStatus.RUNNING)
    third = jm.Job(title="Third", album_id="3", created_at=300.0)
    second = jm.Job(title="Second", album_id="2", created_at=200.0)
    for job in (started, third, second):
        job_persistence.persist(job)
    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    monkeypatch.setattr(jm, "_held_downloads", [])

    jm.restore_jobs({}, requeue=job_runs._requeued_download_run)

    assert jm.registry.get(started.id).status == jm.JobStatus.FAILED
    assert [job.id for job, _run in jm._held_downloads] == [second.id, third.id]
    assert {job.status for job, _run in jm._held_downloads} == {
        jm.JobStatus.PENDING}


def test_persistence_restores_awaiting_review_with_candidates(monkeypatch):
    """The headline reliability win: a completed scan's candidates survive a
    container restart; the user can still approve them instead of re-scanning
    from artist 1."""
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    # Simulate a scan that parked AWAITING_REVIEW before the container died.
    saved = jm.Job(title="Artist scan", artist="Foo")
    saved.kind = "scan"
    saved.execute_kind = "album"
    saved.status = jm.JobStatus.AWAITING_REVIEW
    saved.add_candidate("album", "Bar", "Foo", payload={"album_id": "abc"})
    job_persistence.persist(saved)

    # Drop the in-memory state to mimic the new process.
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


def test_one_broken_review_does_not_abort_job_restore(monkeypatch):
    from qobuz_librarian.web import job_persistence

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


def test_rehydrated_review_never_mints_colliding_cids(monkeypatch):
    """A job rebuilt with pre-existing candidates (restart, tab split) must
    advance its cid counter past them; a fresh c0/c1 colliding with inherited
    rows made a cid-keyed dismiss delete unrelated, even ticked, candidates."""
    from qobuz_librarian.web import job_persistence

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


def test_library_review_rebuilds_from_saved_state_when_no_live_job(
        monkeypatch, tmp_path):
    """With the baseline complete but no live library job (swept cancel,
    discarded scan job, corrupt restart row), the Missing Albums / Gap Fill
    review must rebuild from saved scan state, never a finished status with
    no tabs. Retiring the review (discard / worked-through) blocks the rebuild."""
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
    finally:
        if job is not None:
            _remove_job(job)


def test_missing_batch_allows_an_earlier_sibling_album_to_land(
        tmp_path, monkeypatch):
    """One artist's first download must not stale its next missing album."""
    from copy import deepcopy
    from types import SimpleNamespace

    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import candidate_premise, library_scan_state
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows

    music = tmp_path / "music"
    artist_dir = music / "Artist"
    existing = artist_dir / "Existing"
    existing.mkdir(parents=True)
    (existing / "01.flac").write_bytes(b"existing audio")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "ARTIST_API_DELAY", 0)
    monkeypatch.setattr(cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "review.json")
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
    monkeypatch.setattr(flows, "build_args", lambda: SimpleNamespace())
    monkeypatch.setattr(
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

    monkeypatch.setattr(process_mod, "process_album", process_album)
    monkeypatch.setattr(
        flows, "_refresh_after_local_album_change", lambda *_a, **_k: None)
    monkeypatch.setattr(
        flows, "prune_library_review_candidates", lambda *_a, **_k: 0)

    flows.execute_albums(job, list(job.candidates), "token")

    assert landed == ["First", "Second"]
    assert job.status is not jm.JobStatus.FAILED and not job.error


def test_whole_review_download_retires_and_reparks_failures(monkeypatch, tmp_path):
    """A whole review retires successes and re-parks failures for retry."""
    from qobuz_librarian.library import library_scan_state as lss
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows

    _allow_legacy_candidate_execution(monkeypatch)

    original = lss.load()
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
        assert lss.load().get("review_retired_reason") == "worked_through"
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
        lss._write_state(original)
        _remove_job(running)
        if parked is not None:
            _remove_job(parked)


def test_upgrade_auth_loss_after_first_success_reparks_unstarted(
        monkeypatch, tmp_path):
    from qobuz_librarian.api.auth import AuthLost
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows, job_persistence

    _allow_legacy_candidate_execution(monkeypatch)
    monkeypatch.setattr(job_persistence, "_persist_locked", lambda _job: True)
    monkeypatch.setattr(flows.cfg, "ARTIST_API_DELAY", 0)
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "_refresh_after_local_album_change",
                        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(flows, "prune_library_review_candidates",
                        lambda *_args, **_kwargs: 0)

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
    monkeypatch.setattr(jm.job_persistence, "_persist_locked", lambda _job: True)
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


def test_mode_handoff_to_cli_pauses_web_downloads(client, monkeypatch):
    from qobuz_librarian.web import runtime
    # No active job (the registry is a shared singleton across tests).
    monkeypatch.setattr(jm.registry, "pending_and_running",
                        lambda: [])
    r = client.post("/settings/mode", data={"target": "cli"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings?mode=cli"
    assert runtime._CLI_MODE is True
    # Download and scan endpoints are paused.
    blocked = client.post("/download", data={"album_id": "123"},
                          follow_redirects=False)
    assert blocked.status_code == 503
    # Resume restores web mode.
    back = client.post("/settings/mode", data={"target": "web"},
                       follow_redirects=False)
    assert back.status_code == 303 and back.headers["location"] == "/settings?mode=web"
    assert runtime._CLI_MODE is False


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


def test_a_signed_in_browser_is_never_locked_out(monkeypatch, tmp_path):
    """Behind a proxy every visitor can arrive as the same address, so a
    stranger's guesses must not shut the owner's own session out."""
    from qobuz_librarian.web import auth as web_auth

    monkeypatch.setattr(web_auth, "_login_failures", {})
    monkeypatch.setattr(web_auth, "_user_failures", {})
    monkeypatch.setattr(web_auth, "_login_pending", {})
    monkeypatch.setattr(web_auth, "_user_paused_until", {})

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


def _mutation_paths(app):
    """Concrete inert paths for every unsafe route in the live route table."""
    return sorted(
        {
            route.path.replace("{job_id}", "guard-probe")
            for route in app.routes
            if "POST" in (getattr(route, "methods", None) or set())
        }
    )


def test_every_mutation_route_requires_csrf(monkeypatch):
    from qobuz_librarian.web import app as app_mod

    monkeypatch.setenv("WEB_AUTH", "none")
    with _SameThreadASGIClient(app_mod.app) as client:
        for path in _mutation_paths(app_mod.app):
            client.cookies.clear()
            response = client.post(path, follow_redirects=False)
            assert response.status_code == 403, path


def test_every_non_auth_mutation_route_requires_a_session(monkeypatch, tmp_path):
    from qobuz_librarian.web import app as app_mod

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


def test_a_password_set_in_settings_survives_a_restart(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import auth as web_auth

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


def test_login_rejects_wrong_password(monkeypatch, tmp_path):
    with _enable_auth(monkeypatch, tmp_path) as c:
        c.get("/login")
        tok = c.cookies.get("ql_csrf")
        r = c.post("/login",
                   data={"username": "admin", "password": "nope",
                         "_csrf_token": tok},
                   headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/login?error=")
        assert "ql_session" not in r.cookies
        # Still locked out afterwards.
        assert c.get("/", follow_redirects=False).status_code == 303


def test_login_next_cannot_leave_the_app(monkeypatch, tmp_path):
    # The next field is attacker-writable (it rides links and the login form),
    # so anything that could land off-site or loop must fall back to "/".
    from qobuz_librarian.web import auth as web_auth

    for bad in ("//evil.example", "/\\evil.example"):
        assert web_auth.safe_next_path(bad) == "", bad
    assert web_auth.safe_next_path("/queue") == "/queue"
    assert web_auth.safe_next_path("/jobs/abc?x=1") == "/jobs/abc?x=1"

    with _enable_auth(monkeypatch, tmp_path) as c:
        c.get("/login")
        tok = c.cookies.get("ql_csrf")
        r = c.post("/login",
                   data={"username": "admin", "password": "hunter2hunter2!",
                         "_csrf_token": tok, "next": "//evil.example"},
                   headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"


def test_malformed_host_cannot_bypass_auth(client, monkeypatch, tmp_path):
    # A name rebound to this server reaches nothing, even with sign-in off.
    assert client.get("/queue", headers={"Host": "rebind.attacker.example"}).status_code == 400
    # CVE-2026-48710: Starlette rebuilds request.url.path from the client Host
    # header, so a host like "example.com/login?x=" can make the auth
    # middleware read the path as "/login" and wave a protected route through
    # with no session.
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


def test_password_rotation_rejects_old_sessions_after_failed_session_save(tmp_path, monkeypatch):
    """A stale session file must not revive prior-password access on restart."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import auth as web_auth

    monkeypatch.setattr(cfg, "WEB_AUTH_FILE", tmp_path / "auth.json")
    monkeypatch.setattr(web_auth, "_SESSIONS_FILE", tmp_path / "sessions.json")
    with web_auth._sessions_lock:
        original_sessions = dict(web_auth._sessions)
        web_auth._sessions = {}
    try:
        assert web_auth.set_credentials("admin", "first secure password")
        old_token = web_auth.mint_session()
        assert web_auth.verify_session(old_token)
        # Signing one browser out leaves another signed in.
        one, other = web_auth.mint_session(), web_auth.mint_session()
        web_auth.revoke_session(one)
        assert not web_auth.verify_session(one) and web_auth.verify_session(other)

        # Credential publication succeeds, but invalidating the durable session
        # file does not. Simulate process reconstruction from that stale file.
        monkeypatch.setattr(web_auth, "_save_sessions_locked", lambda: False)
        assert web_auth.set_credentials("admin", "second secure password")
        with web_auth._sessions_lock:
            web_auth._sessions = web_auth._load_sessions()

        assert web_auth.verify_session(old_token) is False
    finally:
        with web_auth._sessions_lock:
            web_auth._sessions = original_sessions


def test_restore_backup_rejects_path_shaped_names(client, tmp_path, monkeypatch):
    # The Restore form posts a bare directory name; anything path-shaped is a
    # probe, not a backup the diagnostics list rendered, so it must not resolve
    # outside the backup dir or restore anything.
    from qobuz_librarian import config as cfg
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    (tmp_path / "backups").mkdir()
    r = client.post("/backups/restore", data={"backup": "../../etc"})
    assert r.status_code == 200
    assert 'data-flash-kind="error"' in r.text


def test_restore_backup_moves_the_files_home(client, tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import backup as backup_mod
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path / "music")
    origin = tmp_path / "music" / "Artist" / "Album (2020)"
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

        # Discard keeps a backup whose files differ from the library's copy.
        kept = backup_mod.backup_album_dir(origin)
        origin.mkdir(parents=True)
        (origin / "01 - Song.flac").write_bytes(b"changed")
        assert client.post("/backups/discard", data={"backup": kept.name}).status_code == 200
        assert (kept.path / "01 - Song.flac").read_bytes() == b"data"
    finally:
        _remove_job(job)


def test_refresh_folds_into_parked_library_review(monkeypatch):
    from qobuz_librarian.web import job_persistence, review_badges, scans

    badge_calls = []
    monkeypatch.setattr(
        review_badges,
        "mark_ready",
        lambda surface: badge_calls.append(surface),
    )
    monkeypatch.setattr(job_persistence, "_persist_locked", lambda _job: True)

    parked = jm.Job(title="Library scan")
    parked.execute_kind = "library"
    parked.add_candidate(
        kind="album",
        title="Dummy",
        artist="Portishead",
        detail="1994 · 16-bit/44.1 kHz · 11 tracks",
        payload={"album_id": "al1"},
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
    scan.add_candidate(
        kind="album",
        title="Roseland NYC Live",
        artist="Portishead",
        detail="1998 · 16-bit/44.1 kHz · 11 tracks",
        payload={"album_id": "al3"},
        selected=False,
    )
    jm.registry.add(scan)
    changed_scan = rescan = None
    try:
        scans._fold_into_parked_library_review(scan)

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

        # A finished scan that derived Portishead again settles the rows it
        # no longer offers there; another artist's rows are left alone.
        parked.add_candidate(
            kind="album",
            title="Mezzanine",
            artist="Massive Attack",
            detail="1998 · 16-bit/44.1 kHz · 11 tracks",
            payload={"album_id": "ma1"},
            selected=False,
        )
        rescan = jm.Job(title="Library scan")
        rescan.execute_kind = "library"
        rescan.status = jm.JobStatus.SCANNING
        rescan.add_candidate(
            kind="album",
            title="Dummy",
            artist="Portishead",
            detail="1994 · 16-bit/44.1 kHz · 11 tracks",
            payload={"album_id": "al1"},
            selected=False,
        )
        rescan.scan_coverage = (frozenset({"Portishead"}), False)
        jm.registry.add(rescan)

        scans._fold_into_parked_library_review(rescan)

        assert [c["payload"]["album_id"] for c in parked.candidates] == [
            "al1", "ma1"]
        assert parked.candidates[0]["selected"] is True
    finally:
        _remove_job(parked)
        _remove_job(scan)
        if changed_scan is not None:
            _remove_job(changed_scan)
        if rescan is not None:
            _remove_job(rescan)


def test_a_missing_column_is_added_whatever_the_version_stamp_says(
        monkeypatch, tmp_path):
    """A database can carry the current version stamp and still be missing a
    column, and every persist() against it then fails silently behind
    _note_write_failure, so the stamp cannot gate the check.
    """
    import sqlite3

    from qobuz_librarian.web import job_persistence

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
    album = {"id": "a0", "title": "Room 25", "tracks_count": 8,
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
