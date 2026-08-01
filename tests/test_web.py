"""Tests for the web UI: background job system (jobs.py) and HTTP routes (app.py).

Trimmed to a maintainable representative set: data-safety paths (restore,
hide/restore round-trip, migration move-vs-copy, persist-without-tearing),
auth/session/CSRF, the run-lock destructive-route guard, settings save/load,
one search + one approve endpoint, and a few genuinely tricky bits of logic.
"""
import asyncio
import concurrent.futures
import threading
import time
from pathlib import Path

import httpx
import pytest

from qobuz_librarian.web import jobs as jm

# ── jobs.py: Job ──────────────────────────────────────────────────────────────


def test_log_lines_capped_with_truncation_marker():
    job = jm.Job()
    total = jm.Job.LOG_CAP + jm.Job._LOG_SLACK + 1
    for i in range(total):
        job.push_line(f"line{i}")
    assert len(job.log_lines) == jm.Job.LOG_CAP
    assert job.log_lines[0] == jm.Job._TRUNCATION_MARKER
    assert job.log_lines[-1] == f"line{total - 1}"


# ── jobs.py: worker loop ──────────────────────────────────────────────────────

def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


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


def test_submit_refuses_job_without_durable_admission(monkeypatch):
    import queue

    from qobuz_librarian.web import jobs as jobs_mod

    registry = jobs_mod.JobRegistry()
    work = queue.Queue()
    job = jobs_mod.Job(id="unsaved-job", title="Album", album_id="album-1")
    monkeypatch.setattr(jobs_mod, "registry", registry)
    monkeypatch.setattr(jobs_mod, "_download_queue", work)
    monkeypatch.setattr(
        jobs_mod.job_persistence,
        "admit",
        lambda _job: False,
        raising=False,
    )

    assert jobs_mod.submit(job, lambda _job: None) is None
    assert registry.get(job.id) is None
    assert work.empty()


def test_late_cancel_does_not_discard_a_parked_review():
    # A cancel flag arriving just as a scan parks its results must not flip
    # AWAITING_REVIEW to CANCELED — the found candidates would be lost.
    job = jm.Job(title="scan")
    job.status = jm.JobStatus.RUNNING
    job.cancel_requested = True

    def fn(j):
        j.add_candidate("album", "Album A", "Artist", payload={"id": 1})
        j.status = jm.JobStatus.AWAITING_REVIEW

    jm._run_task(job, fn)
    assert job.status == jm.JobStatus.AWAITING_REVIEW
    assert len(job.candidates) == 1


def test_per_artist_rescan_supersedes_only_that_artists_parked_review(
        monkeypatch):
    # Two artists each have a scan parked for review.
    from qobuz_librarian.web import app as app_mod

    class Authority:
        @staticmethod
        def intact():
            return True

    monkeypatch.setattr(app_mod, "_RUN_LOCK_HANDLE", Authority())

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
    app_mod._submit_scan_deduped(fresh, noop_scan, noop_exec, "album")
    assert a.status == jm.JobStatus.AWAITING_REVIEW
    assert b.status == jm.JobStatus.AWAITING_REVIEW

    rescan = jm.Job(title="Artist scan", artist="Artist A")
    rescan.execute_kind = "album"
    app_mod._submit_scan_deduped(rescan, noop_scan, noop_exec, "album")
    assert a.status == jm.JobStatus.CANCELED
    assert b.status == jm.JobStatus.AWAITING_REVIEW


def test_download_dedup_respects_new_edition_and_single_track_intent():
    # Folding a /download onto an in-flight job must respect intent, not just the
    # album id: "get this edition too" is a deliberate extra copy and a one-track
    # grab is its own thing — neither should be swallowed by an unrelated job for
    # the same album, and a full-album download must not fold onto a one-track grab.
    from qobuz_librarian.web import app as app_mod

    full = jm.Job(title="Album X", artist="Artist", album_id="X")
    full.status = jm.JobStatus.RUNNING
    jm.registry.add(full)

    assert app_mod._duplicate_download_job("X") is full
    assert app_mod._duplicate_download_job("X", as_new_edition=True) is None
    assert app_mod._duplicate_download_job("X", track_id="42") is None

    grab = jm.Job(title="One track", artist="Artist", album_id="Y")
    grab.single = {"album_id": "Y", "track_id": "7"}
    grab.status = jm.JobStatus.RUNNING
    jm.registry.add(grab)

    assert app_mod._duplicate_download_job("Y", track_id="7") is grab
    assert app_mod._duplicate_download_job("Y", track_id="8") is None
    assert app_mod._duplicate_download_job("Y") is None


def test_new_release_review_never_owns_the_library_surface():
    # New-release results live on their own job page; a parked check must not
    # displace the Missing Albums / Gap Fill review on /library.
    from qobuz_librarian.web import app as app_mod

    library = jm.Job(title="Library scan")
    library.execute_kind = "library"
    library.status = jm.JobStatus.AWAITING_REVIEW
    library.created_at = 100.0
    jm.registry.add(library)

    check = jm.Job(title="New-release check")
    check.execute_kind = "new_releases"
    check.status = jm.JobStatus.AWAITING_REVIEW
    check.created_at = 200.0  # newer, would win under most-recent rules
    jm.registry.add(check)

    assert app_mod._library_current_job() is library


def test_parked_review_candidate_does_not_swallow_a_download():
    # An album that merely appears among a parked review's candidates is not
    # queued for anything — refusing an explicit /download with "already
    # queued" over it would be false.
    from qobuz_librarian.web import app as app_mod

    review = jm.Job(title="Library scan")
    review.execute_kind = "library"
    review.status = jm.JobStatus.AWAITING_REVIEW
    review.add_candidate(kind="album", title="Wanted", artist="Artist",
                         payload={"album_id": "Z"}, selected=False)
    jm.registry.add(review)

    assert app_mod._duplicate_download_job("Z") is None
    # Once approved and running, the same album folds again.
    review.status = jm.JobStatus.RUNNING
    assert app_mod._duplicate_download_job("Z") is review


def test_direct_album_download_refreshes_saved_quality_state(
        monkeypatch, tmp_path):
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import app as app_mod
    from qobuz_librarian.web import flows

    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    album = {
        "id": "alb1",
        "title": "Album",
        "artist": {"name": "Artist"},
    }
    calls = []

    monkeypatch.setattr(
        process_mod,
        "process_album",
        lambda *_a, **_k: {
            "imported": True,
            "n_ok": 1,
            "n_fail": 0,
            "result": "downloaded",
            "dir": album_dir,
        },
    )
    monkeypatch.setattr(
        flows,
        "_refresh_after_local_album_change",
        lambda *a, **kw: calls.append((a, kw)),
    )

    job = jm.Job(title="Album", artist="Artist", album_id="alb1")
    app_mod._make_download_run(album, "tok")(job)

    assert job.status != jm.JobStatus.FAILED
    assert len(calls) == 1
    assert calls[0][0][0] is album
    assert Path(calls[0][0][1]["dir"]) == album_dir
    assert calls[0][1] == {
        "fallback_artist": "Artist",
        "token": "tok",
        "args": calls[0][1]["args"],
        "upgrade": True,
        "downsample": True,
    }


def test_direct_single_track_download_refreshes_saved_quality_state(
        monkeypatch, tmp_path):
    import qobuz_librarian.library.catalog as cat_mod
    import qobuz_librarian.queue.builder as builder_mod
    import qobuz_librarian.queue.executor as executor_mod
    from qobuz_librarian.web import app as app_mod
    from qobuz_librarian.web import flows

    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    album = {
        "id": "alb1",
        "title": "Album",
        "year": 2024,
        "artist": {"name": "Artist"},
        "tracks": {"items": [
            {"id": "t1", "title": "One", "track_number": 1},
            {"id": "t2", "title": "Two", "track_number": 2},
        ]},
    }
    track = album["tracks"]["items"][0]
    calls = []

    monkeypatch.setattr(cat_mod, "find_existing_tracks", lambda *_a, **_k: ([], None))
    monkeypatch.setattr(
        builder_mod,
        "_build_queue_item",
        lambda **kwargs: {
            "album": kwargs["album"],
            "missing": kwargs["missing"],
            "n_ok": 0,
            "n_fail": 0,
            "imported": False,
        },
    )

    def fake_execute(queue, *_a, **_k):
        queue[0]["n_ok"] = 1
        queue[0]["n_fail"] = 0
        queue[0]["imported"] = True
        queue[0]["_resolved_post_dir"] = str(album_dir)

    monkeypatch.setattr(executor_mod, "_execute_download_queue", fake_execute)
    monkeypatch.setattr(
        flows,
        "_refresh_after_local_album_change",
        lambda *a, **kw: calls.append((a, kw)),
    )
    monkeypatch.setattr(app_mod.cfg, "SUPPRESS_SINGLE_TRACK_GAPS", False, raising=False)

    job = jm.Job(title="One", artist="Artist", album_id="alb1")
    app_mod._make_single_track_run(album, track, "tok")(job)

    assert job.status != jm.JobStatus.FAILED
    assert len(calls) == 1
    assert calls[0][0][0] is album
    assert Path(calls[0][0][1]["dir"]) == album_dir
    assert calls[0][1] == {
        "fallback_artist": "Artist",
        "token": "tok",
        "args": calls[0][1]["args"],
        "upgrade": True,
        "downsample": True,
    }


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


def test_approve_flips_status_then_passes_chosen_to_execute(monkeypatch):
    job = jm.Job(title="scan-approve")
    job.kind = "scan"
    job.status = jm.JobStatus.AWAITING_REVIEW
    got_chosen = []
    job._execute_fn = lambda j, chosen: got_chosen.append(chosen)
    job.add_candidate("album", "A", "Artist", payload={"id": 1})

    status_at_put = []
    enqueued = []

    def _spy_put(item):
        status_at_put.append(item[0].status)
        enqueued.append(item[1])

    monkeypatch.setattr(jm._scan_queue, "put", _spy_put)

    assert jm.approve(job, ["c0"]) is True
    # Status flips to PENDING before the execute step is enqueued, so a second
    # concurrent approve can't double-enqueue the download.
    assert status_at_put == [jm.JobStatus.PENDING]
    # Running the enqueued step hands execute_fn exactly the kept candidate.
    enqueued[0](job)
    assert [c["payload"] for c in got_chosen[0]] == [{"id": 1}]
    # A second approve no longer sees AWAITING_REVIEW, so it's rejected.
    assert jm.approve(job, ["c0"]) is False


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


def _run_web_executors_inline(monkeypatch, app_mod):
    monkeypatch.setattr(app_mod, "asyncio", _InlineExecutorAsyncio(asyncio))


@pytest.fixture
def client(monkeypatch):
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )
    from qobuz_librarian.web import app as app_mod

    class TestAuthority:
        def __init__(self):
            self.closed = False

        def intact(self):
            return not self.closed

        def close(self):
            self.closed = True

    monkeypatch.setattr(app_mod, "_RUN_LOCK_HANDLE", TestAuthority())
    monkeypatch.setattr(app_mod, "_CLI_MODE", False)
    monkeypatch.setattr(app_mod, "_LOCK_BUSY_PID", None)
    monkeypatch.setattr(app_mod, "_LOCK_UNENFORCEABLE", False)
    monkeypatch.setattr(app_mod, "_SHUTTING_DOWN", False)
    # This lightweight client bypasses the application lifespan. Treat its
    # in-memory registry as already restored unless a test exercises restore.
    monkeypatch.setattr(app_mod, "_JOBS_RESTORED", True)
    clear_recovery = StartupRecoveryResult(StartupRecoveryStatus.CLEAR)
    monkeypatch.setattr(app_mod, "_STARTUP_RECOVERY_RESULT", clear_recovery)
    monkeypatch.setattr(app_mod, "_STARTUP_RECOVERY_UNKNOWN", False)

    def _record_clear(_authority):
        app_mod._STARTUP_RECOVERY_RESULT = clear_recovery
        return clear_recovery

    monkeypatch.setattr(app_mod, "_record_startup_recovery", _record_clear)
    _run_web_executors_inline(monkeypatch, app_mod)
    with _SameThreadASGIClient(app_mod.app) as c:
        c.get("/queue")
        token = c.cookies.get("ql_csrf")
        c.headers.update({"X-CSRF-Token": token})
        yield c


def test_search_uses_a_generous_result_limit(client, monkeypatch):
    # The front-page search was capped at 8, so a major artist surfaced almost
    # nothing (the owner's first complaint).
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian import config as cfg
    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    seen = {}

    def fake(q, t, limit=None):
        seen["limit"] = limit
        return []

    monkeypatch.setattr(search_mod, "search_albums", fake)
    r = client.post("/search", data={"q": "Paul McCartney", "kind": "album"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert seen.get("limit") == cfg.SEARCH_LIMIT
    assert cfg.SEARCH_LIMIT >= 20


def test_artist_search_lists_qobuz_artists(client, monkeypatch):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian import config as cfg

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    seen = {}

    def fake(q, t, limit=None):
        seen["query"] = q
        seen["limit"] = limit
        return [{"id": "artist1", "name": "Paysage d'Hiver", "albums_count": 12}]

    monkeypatch.setattr(search_mod, "search_artists", fake)

    r = client.post("/search", data={"q": "Paysage", "kind": "artist"},
                    headers={"HX-Request": "true"})

    assert r.status_code == 200
    assert seen == {"query": "Paysage", "limit": cfg.ARTIST_LOOKUP_LIMIT}
    assert "Paysage" in r.text
    assert "View albums" in r.text
    assert 'name="artist_id" value="artist1"' in r.text


def test_artist_search_selected_artist_shows_discography(client, monkeypatch):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as catalog_mod
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian import config as cfg

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(catalog_mod, "find_album_dir_filesystem", lambda _a: None)
    seen = {}

    def fake_catalog(artist_id, token, limit=None, fresh=False):
        seen["artist_id"] = artist_id
        seen["limit"] = limit
        return ([{
            "id": "album1",
            "title": "Das Tor",
            "artist": {"name": "Paysage d'Hiver"},
            "year": 2013,
            "tracks_count": 10,
            "maximum_bit_depth": 16,
        }], 1)

    monkeypatch.setattr(search_mod, "get_artist_albums", fake_catalog)

    r = client.post(
        "/search",
        data={
            "q": "Paysage",
            "kind": "artist",
            "artist_id": "artist1",
            "artist_name": "Paysage d'Hiver",
        },
        headers={"HX-Request": "true"},
    )

    assert r.status_code == 200
    assert seen == {"artist_id": "artist1", "limit": cfg.ARTIST_CATALOG_LIMIT}
    assert "Paysage d&#39;Hiver" in r.text
    assert "1 album on Qobuz" in r.text
    assert "Das Tor" in r.text
    assert "Download" in r.text


def test_dashboard_artist_query_renders_initial_results(client, monkeypatch):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian import config as cfg

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    seen = {}

    def fake(q, t, limit=None):
        seen["query"] = q
        seen["limit"] = limit
        return [{"id": "artist1", "name": "Paysage d'Hiver", "albums_count": 12}]

    monkeypatch.setattr(search_mod, "search_artists", fake)

    r = client.get("/?kind=artist&q=Paysage")

    assert r.status_code == 200
    assert seen == {"query": "Paysage", "limit": cfg.ARTIST_LOOKUP_LIMIT}
    assert "Paysage d&#39;Hiver" in r.text
    assert "View albums" in r.text


def test_album_search_keeps_upgrades_out_of_search(client, monkeypatch, tmp_path):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as catalog_mod
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    album = {
        "id": "album1",
        "title": "Das Tor",
        "artist": {"name": "Paysage d'Hiver"},
        "year": 2013,
        "tracks_count": 10,
        "maximum_bit_depth": 24,
    }
    owned = tmp_path / "Das Tor (2013)"
    owned.mkdir()
    for n in range(1, 11):
        (owned / f"{n:02d} - Das Tor.flac").write_bytes(b"\x00")

    monkeypatch.setattr(search_mod, "search_albums", lambda *_a, **_kw: [album])
    monkeypatch.setattr(catalog_mod, "find_album_dir_filesystem", lambda _a: owned)

    r = client.post("/search", data={"q": "Das Tor", "kind": "album"},
                    headers={"HX-Request": "true"})

    assert r.status_code == 200
    assert "In library" in r.text
    assert "quality-upgrade" not in r.text
    assert ">Upgrade<" not in r.text


def test_album_search_marks_a_part_finished_album_as_partial(client, monkeypatch, tmp_path):
    # One file in the folder is not the album. Calling it "In library" took away
    # the checkbox and the download button on exactly the albums gap fill exists
    # to finish.
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as catalog_mod
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    album = {
        "id": "album1",
        "title": "Das Tor",
        "artist": {"name": "Paysage d'Hiver"},
        "year": 2013,
        "tracks_count": 10,
        "maximum_bit_depth": 24,
    }
    partial = tmp_path / "Das Tor (2013)"
    partial.mkdir()
    (partial / "01 - Das Tor.flac").write_bytes(b"\x00")

    monkeypatch.setattr(search_mod, "search_albums", lambda *_a, **_kw: [album])
    monkeypatch.setattr(catalog_mod, "find_album_dir_filesystem", lambda _a: partial)

    r = client.post("/search", data={"q": "Das Tor", "kind": "album"},
                    headers={"HX-Request": "true"})

    assert r.status_code == 200
    assert "In library" not in r.text
    assert "1 of 10" in r.text
    assert 'name="album_id" value="album1"' in r.text   # still downloadable


def test_new_release_check_refused_without_baseline(client, monkeypatch):
    # "Check for new releases" is a library-walk-and-compare — useless until a
    # full library scan has built the baseline.
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian.library import new_releases
    from qobuz_librarian.web import flows
    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(flows, "scan_new_releases", lambda *a, **k: None)
    assert new_releases.is_baseline_complete() is False      # fresh state, no baseline
    r = client.post("/library", data={"mode": "new_releases"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/library")
    assert app_mod._existing_new_release_check() is None     # no crawl was started


def test_library_scan_state_explains_empty_music_root(tmp_path, monkeypatch):
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod.cfg, "MUSIC_ROOT", tmp_path)
    state = app_mod._library_scan_state()

    assert state["ready"] is False
    assert str(tmp_path) in state["message"]
    assert "MUSIC_ROOT" not in state["message"]
    assert "QL_MUSIC_DIR" not in state["message"]
    assert "artist" in state["message"].lower()


def test_qobuz_ready_false_when_saved_token_is_rejected(monkeypatch):
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_read_creds",
                        lambda: {"auth_token": "bad-token", "user_id": "user"})
    monkeypatch.setattr(app_mod, "_TOKEN_VALID", False)

    assert app_mod._qobuz_ready() is False


def test_qobuz_ready_allows_unproven_saved_token(monkeypatch):
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_read_creds",
                        lambda: {"auth_token": "maybe-token", "user_id": "user"})
    monkeypatch.setattr(app_mod, "_TOKEN_VALID", None)

    assert app_mod._qobuz_ready() is True


def test_settings_save_defers_apply_when_job_is_active(tmp_path, monkeypatch):
    """An in-flight job must not see cfg.* flip mid-run."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(cfg, "DOWNSAMPLE_HIRES_ENABLED", False)
    monkeypatch.setattr(ss, "_any_active_job", lambda: True)
    with ss._pending_lock:
        ss._pending_apply = None

    ok, _ = ss.save({"DOWNSAMPLE_HIRES_ENABLED": True})
    assert ok is True
    assert (tmp_path / "s.json").exists()
    assert cfg.DOWNSAMPLE_HIRES_ENABLED is False  # not yet applied

    ss.drain_pending()
    assert cfg.DOWNSAMPLE_HIRES_ENABLED is True
    ss.drain_pending()
    assert cfg.DOWNSAMPLE_HIRES_ENABLED is True  # idempotent


def test_parked_review_does_not_defer_settings(tmp_path, monkeypatch):
    """A parked review can sit for weeks — a save made next to one must apply
    right away, not wait in the pending slot for a job that may never run."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(cfg, "DOWNSAMPLE_HIRES_ENABLED", False)
    review = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    review.execute_kind = "downsample"
    with ss._pending_lock:
        ss._pending_apply = None

    assert ss._any_active_job() is False
    ok, _ = ss.save({"DOWNSAMPLE_HIRES_ENABLED": True})
    assert ok is True
    assert cfg.DOWNSAMPLE_HIRES_ENABLED is True  # applied immediately
    with ss._pending_lock:
        assert ss._pending_apply is None


def test_quality_change_flags_the_stale_upgrade_review(
        client, tmp_path, monkeypatch):
    """Lowering/raising the download quality leaves a saved Upgrade
    review promising dead targets — the save must say a refresh updates it.
    An unchanged save stays quiet."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 4)
    monkeypatch.setattr(ss, "_any_active_job", lambda: False)
    monkeypatch.setattr("qobuz_librarian.quality.upgrade_state.load",
                        lambda: {"candidates": [{"title": "x"}]})
    with ss._pending_lock:
        ss._pending_apply = None

    r = client.post("/settings/behavior", data={"STREAMRIP_QUALITY": "2"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "quality_note=1" in r.headers["location"]
    r2 = client.post("/settings/behavior", data={"STREAMRIP_QUALITY": "2"},
                     follow_redirects=False)
    assert "quality_note" not in r2.headers["location"]


def test_settings_save_only_pins_changed_fields(tmp_path, monkeypatch):
    """Saving the Settings form must not freeze untouched fields into the
    settings file — the file wins over env on load, so writing a field that
    merely matched its current value would silently stop that env var from
    ever applying again. Only real changes (and fields saved before) persist."""
    import json

    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(cfg, "LYRICS_ENABLED", True)
    monkeypatch.setattr(cfg, "PREFER_HIRES", True)
    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 4)
    monkeypatch.setattr(ss, "_any_active_job", lambda: False)
    with ss._pending_lock:
        ss._pending_apply = None

    # The form posts every field; only LYRICS_ENABLED actually changed.
    ok, _ = ss.save({"LYRICS_ENABLED": False, "PREFER_HIRES": True,
                     "STREAMRIP_QUALITY": "4"})
    assert ok is True
    on_disk = json.loads((tmp_path / "s.json").read_text())
    assert on_disk == {"LYRICS_ENABLED": False}

    # A field that was saved before stays in the file even when a later save
    # posts it unchanged — the user set it deliberately, so it keeps winning.
    ok, _ = ss.save({"LYRICS_ENABLED": False, "PREFER_HIRES": False})
    on_disk = json.loads((tmp_path / "s.json").read_text())
    assert on_disk == {"LYRICS_ENABLED": False, "PREFER_HIRES": False}


# ── CSRF middleware ───────────────────────────────────────────────────────────

def test_csrf_post_without_token_is_rejected():
    """One representative POST verifies CSRF-missing → 403."""
    from qobuz_librarian.web import app as app_mod
    with _SameThreadASGIClient(app_mod.app) as c:
        c.get("/queue")
        r = c.post("/search", data={"q": "anything"})
        assert r.status_code == 403


# ── run-lock busy → destructive routes 503, read-only stay open ───────

def test_lock_busy_refuses_destructive_routes(monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", True, raising=False)
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(webapp, "_TOKEN_VALID", True)
    _run_web_executors_inline(monkeypatch, webapp)
    with _SameThreadASGIClient(webapp.app) as c:
        c.get("/queue")
        token = c.cookies.get("ql_csrf")
        c.headers.update({"X-CSRF-Token": token})
        monkeypatch.setattr(webapp, "_LOCK_BUSY_PID", 4321)

        dash = c.get("/")
        assert dash.status_code == 200
        assert "Another Qobuz Librarian run is active." in dash.text
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
            # The full-page response should render the base shell, not a bare
            # <pre>, so a non-htmx caller still has navigation back.
            assert "ql-app-shell" in r.text, f"{path} should render base.html shell"
            assert "Another Qobuz Librarian run is active." in r.text
            assert "pid 4321" not in r.text
            assert "run-lock" not in r.text
            assert ">Try again</button>" in r.text
            assert ">Back to Search</a>" in r.text


def test_folder_move_recovery_pause_names_cause_and_exact_paths(
        client, monkeypatch, tmp_path):
    from qobuz_librarian.library.post_import_relocation import (
        RelocationRecoveryResult,
        RelocationRecoveryStatus,
    )
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )
    from qobuz_librarian.web import app as webapp

    affected_paths = (
        tmp_path / "music" / "Artist One" / "Album One",
        tmp_path / "music" / "Artist Two" / "Album Two",
    )
    monkeypatch.setattr(webapp, "_STARTUP_RECOVERY_UNKNOWN", False)
    monkeypatch.setattr(
        webapp,
        "_STARTUP_RECOVERY_RESULT",
        StartupRecoveryResult(
            StartupRecoveryStatus.ATTENTION_REQUIRED,
            reason="post-import-relocation-unsettled",
            post_import_relocation=RelocationRecoveryResult(
                RelocationRecoveryStatus.ATTENTION_REQUIRED,
                "exact relocation evidence changed",
                affected_paths,
            ),
        ),
    )

    blocked = client.post(
        "/download", data={"album_id": "1"}, follow_redirects=False
    )

    assert blocked.status_code == 503
    assert "interrupted library-folder move" in blocked.text
    assert "exact relocation evidence changed" in blocked.text
    assert "Paths needing attention" in blocked.text
    assert all(str(path) in blocked.text for path in affected_paths)
    assert "Post-import folder-move recovery needs attention" in blocked.text
    assert "interrupted download" not in blocked.text


def test_nav_shows_qobuz_setup_when_credentials_are_missing(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds", lambda: {})

    r = client.get("/")

    assert r.status_code == 200
    assert "Set up Qobuz in Settings" in r.text
    assert 'href="/settings"' in r.text
    assert "Your Qobuz token was rejected" not in r.text


def test_dashboard_qobuz_setup_card_stays_until_qobuz_is_connected(
        client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds", lambda: {})

    r = client.get("/")

    assert r.status_code == 200
    assert 'data-qobuz-setup-card' in r.text
    assert 'data-qobuz-setup-dismiss' not in r.text
    assert "Qobuz credentials" in r.text
    assert "Open Settings" in r.text
    assert "Downsample" in r.text
    assert "Lyrics" in r.text
    assert "Set up Qobuz in Settings" in r.text


def test_search_page_does_not_show_empty_dashboard_cards(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})

    r = client.get("/")

    assert r.status_code == 200
    assert "<h1>Search</h1>" in r.text
    assert 'class="ql-search-form"' in r.text
    assert "Needs review" not in r.text
    assert "Running and queued" not in r.text
    assert "Recent downloads" not in r.text
    assert "Latest completed downloads" not in r.text
    assert "No scans waiting for review." not in r.text
    assert "Nothing running or queued." not in r.text


def test_search_page_does_not_render_review_jobs_as_front_page_cards(
        client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
    job.execute_kind = "library"
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      payload={"year": "1994"}, selected=False)
    try:
        r = client.get("/")

        assert r.status_code == 200
        assert 'class="ql-search-form"' in r.text
        assert "Library scan" not in r.text
        assert "1 missing album or Gap Fill candidate found." not in r.text
        assert f'href="/jobs/{job.id}"' not in r.text
    finally:
        _remove_job(job)


def test_upgrade_disabled_hides_nav_and_badge(client, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import review_badges

    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", False, raising=False)
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    review_badges.mark_ready("upgrade", now=100.0)

    r = client.get("/")

    assert r.status_code == 200
    assert 'href="/upgrade"' not in r.text
    assert 'data-review-badge="upgrade"' not in r.text


def test_upgrade_disabled_page_redirects_cleanly(client, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", False, raising=False)
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})

    r = client.get("/upgrade", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_upgrade_page_reviews_saved_baseline_candidates(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(
        "qobuz_librarian.quality.upgrade_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": True,
            "candidates": [{
                "title": "Dummy",
                "artist": "Portishead",
                "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                "payload": {"album_id": "up1", "year": "1994", "cover": ""},
            }],
        },
    )

    r = client.get("/upgrade")

    assert r.status_code == 200
    assert "upgrade candidate" in r.text
    assert "1 upgrade candidate" in r.text
    assert 'action="/upgrade/review"' in r.text
    assert "Review candidates" in r.text
    assert "Start upgrade scan" not in r.text
    assert "Quality upgrade scan" not in r.text


def test_upgrade_review_post_uses_saved_state_without_scanning(client, monkeypatch):
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(
        "qobuz_librarian.quality.upgrade_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": True,
            "candidates": [{
                "title": "Dummy",
                "artist": "Portishead",
                "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                "payload": {"album_id": "up1", "year": "1994", "cover": ""},
            }],
        },
    )

    def fail_scan(*_args, **_kwargs):
        raise AssertionError("Upgrade review must not start a scan")

    monkeypatch.setattr("qobuz_librarian.web.flows.scan_upgrades", fail_scan)

    r = client.post("/upgrade/review", follow_redirects=False)

    assert r.status_code == 303
    job_id = r.headers["location"].removeprefix("/jobs/")
    job = job_mgr.registry.get(job_id)
    assert job is not None
    assert job.status == job_mgr.JobStatus.AWAITING_REVIEW
    assert job.execute_kind == "upgrade"
    assert job.review_verb == "Upgrade"
    assert len(job.candidates) == 1
    assert job.candidates[0]["selected"] is False


def test_upgrade_review_post_reuses_existing_saved_review_job(
        client, monkeypatch):
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(
        "qobuz_librarian.quality.upgrade_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": True,
            "candidates": [{
                "title": "Dummy",
                "artist": "Portishead",
                "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                "payload": {"album_id": "up1", "year": "1994", "cover": ""},
            }],
        },
    )

    first = client.post("/upgrade/review", follow_redirects=False)
    second = client.post("/upgrade/review", follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 303
    assert second.headers["location"] == first.headers["location"]
    assert len([
        j for j in job_mgr.registry.awaiting_review()
        if j.execute_kind == "upgrade"
    ]) == 1

def test_saved_review_creation_is_atomic_for_parallel_posts(monkeypatch):
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    real_add = job_mgr.registry.add

    def slow_add(job):
        time.sleep(0.02)
        real_add(job)

    monkeypatch.setattr(job_mgr.registry, "add", slow_add)
    state = {
        "complete": True,
        "candidates": [
            {
                "title": "First",
                "artist": "Portishead",
                "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                "payload": {"album_id": "up1"},
            },
            {
                "title": "Second",
                "artist": "Portishead",
                "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                "payload": {"album_id": "up2"},
            },
        ],
    }

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        jobs = list(ex.map(
            lambda _i: webapp._review_job_from_upgrade_state(state),
            range(8),
        ))

    assert len({j.id for j in jobs}) == 1
    assert len([
        j for j in job_mgr.registry.awaiting_review()
        if j.execute_kind == "upgrade"
    ]) == 1

    for candidate in jobs[0].candidates:
        candidate["selected"] = True
    jobs[0].status = job_mgr.JobStatus.RUNNING
    remaining = {
        "complete": True,
        "candidates": [{
            **state["candidates"][1],
            "detail": "fresh saved-state detail",
        }],
    }
    claimed = webapp._review_job_from_upgrade_state(remaining)
    assert claimed is jobs[0]
    assert len([
        j for j in job_mgr.registry.all()
        if j.execute_kind == "upgrade" and j.status in job_mgr.ACTIVE
    ]) == 1


def test_upgrade_saved_review_respects_hidden_candidates(
        client, monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(
        "qobuz_librarian.quality.upgrade_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": True,
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
        },
    )

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
    assert "1 upgrade candidate" in r.text
    assert "2 upgrade candidates" not in r.text

    second = client.post("/upgrade/review", follow_redirects=False)
    assert second.headers["location"] == first.headers["location"]
    assert len([
        j for j in job_mgr.registry.awaiting_review()
        if j.execute_kind == "upgrade"
    ]) == 1


def test_upgrade_saved_review_restore_updates_existing_job(
        client, monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(
        "qobuz_librarian.quality.upgrade_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": True,
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
        },
    )

    first = client.post("/upgrade/review", follow_redirects=False)
    job_id = first.headers["location"].removeprefix("/jobs/")
    job = job_mgr.registry.get(job_id)
    keep = next(c["cid"] for c in job.candidates if c["title"] == "Dummy")
    client.post(f"/jobs/{job.id}/select", data={"cid": keep, "checked": "1"})
    client.post(f"/jobs/{job.id}/hide", data={"artist": "Portishead"})
    hidden.restore_albums(hidden.SCOPE_UPGRADE, [
        hidden.album_fingerprint("Portishead", "Third")
    ])

    second = client.post("/upgrade/review", follow_redirects=False)

    assert second.headers["location"] == first.headers["location"]
    assert [c["title"] for c in job.candidates] == ["Dummy", "Third"]
    assert {c["title"]: c["selected"] for c in job.candidates} == {
        "Dummy": True,
        "Third": False,
    }
    assert len([
        j for j in job_mgr.registry.awaiting_review()
        if j.execute_kind == "upgrade"
    ]) == 1


def test_upgrade_approve_resyncs_saved_review_before_execution(
        client, monkeypatch):
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    state = {
        "updated_at": time.time(),
        "complete": True,
        "candidates": [{
            "title": "Stale",
            "artist": "Portishead",
            "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
            "payload": {"album_id": "old", "year": "1994", "cover": ""},
        }],
    }
    monkeypatch.setattr("qobuz_librarian.quality.upgrade_state.load", lambda: state)

    first = client.post("/upgrade/review", follow_redirects=False)
    job_id = first.headers["location"].removeprefix("/jobs/")
    job = job_mgr.registry.get(job_id)
    client.post(f"/jobs/{job.id}/select",
                data={"cid": job.candidates[0]["cid"], "checked": "1"})
    state["candidates"] = []

    r = client.post(f"/jobs/{job.id}/approve", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == f"/jobs/{job.id}?noselection=1"
    assert job.status == job_mgr.JobStatus.AWAITING_REVIEW
    assert job.candidates == []


def test_approve_refuses_parked_upgrade_review_without_credentials(
        client, monkeypatch):
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    creds = {"auth_token": "dummy", "user_id": "dummy"}
    monkeypatch.setattr(webapp, "_read_creds", lambda: dict(creds))
    monkeypatch.setattr(
        "qobuz_librarian.quality.upgrade_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": True,
            "candidates": [{
                "title": "Dummy",
                "artist": "Portishead",
                "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                "payload": {"album_id": "up1", "year": "1994", "cover": ""},
            }],
        },
    )

    first = client.post("/upgrade/review", follow_redirects=False)
    job_id = first.headers["location"].removeprefix("/jobs/")
    job = job_mgr.registry.get(job_id)
    client.post(f"/jobs/{job.id}/select",
                data={"cid": job.candidates[0]["cid"], "checked": "1"})
    creds.clear()

    r = client.post(f"/jobs/{job.id}/approve", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert job.status == job_mgr.JobStatus.AWAITING_REVIEW
    assert any(c.get("selected") for c in job.candidates)


def test_approve_refuses_parked_library_review_without_credentials(
        client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds", lambda: {})
    job = jm.Job(title="Library scan")
    job.kind = "scan"
    job.execute_kind = "library"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job._execute_fn = lambda j, chosen: None
    job.add_candidate("album", "A", "X", payload={"album_id": "a1"})
    jm.registry.add(job)
    try:
        r = client.post(f"/jobs/{job.id}/approve", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/library?error=")
        assert job.status == jm.JobStatus.AWAITING_REVIEW
        assert job.candidates[0]["selected"]
    finally:
        _remove_job(job)


def test_auth_failure_before_any_import_reparks_the_review():
    """Qobuz dying on the FIRST album of an approved run must not consume the
    review — the picks go back to awaiting-review instead of a failed job."""
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
        assert _wait_for(lambda: any(
            "untouched" in line for line in job.log_lines))
        assert _wait_for(lambda: job.status == jm.JobStatus.AWAITING_REVIEW)
        assert job.candidates[0]["selected"]
        assert job.finished_at is None
        assert job.error is None
    finally:
        _remove_job(job)


def test_auth_failure_after_an_import_keeps_fail_semantics():
    from qobuz_librarian.api.auth import AuthLost

    job = jm.Job(title="Library scan")
    job.kind = "scan"
    job.execute_kind = "library"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job.add_candidate("album", "A", "X", payload={"album_id": "a1"})

    def _dies_late(j, chosen):
        j._imported_any = True
        raise AuthLost("token rejected mid-run")

    job._execute_fn = _dies_late
    jm.registry.add(job)
    try:
        jm.start_worker()
        assert jm.approve(job, None) is True
        assert _wait_for(lambda: job.status == jm.JobStatus.FAILED)
    finally:
        _remove_job(job)


def test_incomplete_upgrade_state_is_not_reviewable(client, monkeypatch):
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(
        "qobuz_librarian.quality.upgrade_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": False,
            "candidates": [{
                "title": "Partial",
                "artist": "Portishead",
                "detail": "stale",
                "payload": {"album_id": "up1"},
            }],
        },
    )

    r = client.post("/upgrade/review", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/upgrade"
    assert job_mgr.registry.awaiting_review() == []


def test_first_downsample_prompts_for_keep_choice_then_saves_it(
        client, monkeypatch, tmp_path):
    """With keep-originals still unchosen, approving a downsample shows the
    one-time prompt instead of rewriting anything; picking one saves it to the
    real setting and the run proceeds, so a returning user is never asked again."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import jobs as job_mgr
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
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
    monkeypatch.setattr(
        "qobuz_librarian.library.downsample_state.load", lambda: state)

    first = client.post("/downsample/review", follow_redirects=False)
    job = job_mgr.registry.get(first.headers["location"].removeprefix("/jobs/"))
    client.post(f"/jobs/{job.id}/select",
                data={"cid": job.candidates[0]["cid"], "checked": "1"})

    r = client.post(f"/jobs/{job.id}/approve", follow_redirects=False)
    assert r.status_code == 200
    assert "Before your first downsample" in r.text
    assert job.status == job_mgr.JobStatus.AWAITING_REVIEW
    assert cfg.DOWNSAMPLE_KEEP_ORIGINALS is None

    r2 = client.post(f"/jobs/{job.id}/approve",
                     data={"keep_choice": "keep"}, follow_redirects=False)
    assert r2.status_code == 303
    assert cfg.DOWNSAMPLE_KEEP_ORIGINALS == "keep"


def test_first_downsample_keep_choice_applies_while_a_job_is_running(
        client, monkeypatch, tmp_path):
    """The keep/delete pick made at the first-downsample prompt must take
    effect for the run it launches even when another job is already active.
    settings_store.save() defers its in-memory apply while a job runs, so the
    approve path applies the choice itself — otherwise the run reads the still
    unset value and deletes the hi-res originals despite a 'keep' choice."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import jobs as job_mgr
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(ss, "_pending_apply", None)
    monkeypatch.setattr(cfg, "DOWNSAMPLE_KEEP_ORIGINALS", None)
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(job_mgr._scan_queue, "put", lambda item: None)
    # A job is running in the other lane, so save() takes its deferral branch —
    # the exact condition that used to strand the choice at its unset default.
    monkeypatch.setattr(ss, "_any_active_job", lambda: True)
    state = {
        "updated_at": time.time(), "complete": True,
        "candidates": [{
            "title": "Album", "artist": "Portishead",
            "detail": "24-bit / 96 kHz -> 16-bit / 48 kHz",
            "album_dir": "/music/Portishead/Album", "est_saving": 1234,
        }],
    }
    monkeypatch.setattr(
        "qobuz_librarian.library.downsample_state.load", lambda: state)

    first = client.post("/downsample/review", follow_redirects=False)
    job = job_mgr.registry.get(first.headers["location"].removeprefix("/jobs/"))
    client.post(f"/jobs/{job.id}/select",
                data={"cid": job.candidates[0]["cid"], "checked": "1"})

    r = client.post(f"/jobs/{job.id}/approve",
                    data={"keep_choice": "keep"}, follow_redirects=False)
    assert r.status_code == 303
    # Applied in-memory immediately despite the deferral: the launched run
    # reads "keep" and parks a restorable backup rather than deleting.
    assert cfg.DOWNSAMPLE_KEEP_ORIGINALS == "keep"


def test_approve_refuses_parked_downsample_review_without_engine(
        client, monkeypatch):
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(
        "qobuz_librarian.library.downsample_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": True,
            "candidates": [{
                "title": "Dummy",
                "artist": "Portishead",
                "detail": "24-bit / 96 kHz -> 16-bit / 48 kHz",
                "album_dir": "/music/Portishead/Dummy",
                "est_saving": 1234,
            }],
        },
    )

    first = client.post("/downsample/review", follow_redirects=False)
    job_id = first.headers["location"].removeprefix("/jobs/")
    job = job_mgr.registry.get(job_id)
    client.post(f"/jobs/{job.id}/select",
                data={"cid": job.candidates[0]["cid"], "checked": "1"})
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", False)

    r = client.post(f"/jobs/{job.id}/approve", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/downsample"
    assert job.status == job_mgr.JobStatus.AWAITING_REVIEW


def test_incomplete_downsample_state_is_not_reviewable(client, monkeypatch):
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(
        "qobuz_librarian.library.downsample_state.load",
        lambda: {
            "updated_at": time.time(),
            "complete": False,
            "candidates": [{
                "title": "Partial",
                "artist": "Portishead",
                "detail": "stale",
                "album_dir": "/music/Portishead/Partial",
                "est_saving": 1234,
            }],
        },
    )

    r = client.post("/downsample/review", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/downsample"
    assert job_mgr.registry.awaiting_review() == []


def test_dashboard_first_run_offers_baseline_scan_with_skip(client, monkeypatch):
    # On first run the dashboard OFFERS the baseline scan (Scan / Skip) rather
    # than auto-starting it.
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import new_releases
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr("qobuz_librarian.library.scanner.list_library_artists",
                        lambda: ["Some Artist"])
    monkeypatch.setattr(cfg, "AUTO_LIBRARY_SCAN", True)
    monkeypatch.setattr(new_releases, "is_baseline_complete", lambda: False)
    monkeypatch.setattr(new_releases, "auto_scan_attempted", lambda: False)

    r = client.get("/")

    assert r.status_code == 200
    assert "Scan library" in r.text
    assert "Builds the Missing Albums, Gap Fill, Upgrade, and Downsample reviews." in r.text
    assert 'action="/library/skip-setup"' in r.text and "Not now" in r.text
    # It's an offer, not an auto-started scan.
    assert "Your baseline scan is running" not in r.text


def test_library_force_full_post_starts_forced_scan(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(webapp, "_library_scan_state",
                        lambda: {"ready": True, "count": 1, "message": ""})
    started = {}

    def fake_start_library_scan(*, partial_only=False, force_full=False):
        job = jm.Job(title="scan")
        started["partial_only"] = partial_only
        started["force_full"] = force_full
        return job

    monkeypatch.setattr(webapp, "_start_library_scan", fake_start_library_scan)

    r = client.post(
        "/library",
        data={"mode": "missing_albums", "force_full": "1"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert started == {"partial_only": False, "force_full": True}


def test_non_htmx_search_post_returns_to_dashboard(client, monkeypatch):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "search_albums", lambda *_a, **_kw: [])

    r = client.post("/search", data={"q": "Paysage d'Hiver"},
                    follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_queue_shows_empty_state_when_only_parked_reviews_exist(client):
    # Parked reviews render on their own surfaces, not in the queue — so a
    # registry holding nothing but a parked review must still show the queue's
    # empty state rather than a blank page (the stack wrapper with no sections).
    review = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Downsample scan")
    review.execute_kind = "downsample"

    r = client.get("/queue")

    assert r.status_code == 200
    assert "Queue is empty." in r.text


def test_history_retry_shows_for_archived_failed_download_too(
        client, monkeypatch):
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()

    archived = jm.Job(title="Archived failure", artist="Portishead",
                      album_id="archived")
    archived.status = jm.JobStatus.FAILED
    archived.finished_at = time.time() - 10
    job_persistence.persist(archived)

    live = jm.Job(title="Live failure", artist="Portishead", album_id="live")
    live.status = jm.JobStatus.FAILED
    live.finished_at = time.time()
    jm.registry.add(live)
    try:
        r = client.get("/queue/history")

        assert r.status_code == 200
        assert f'action="/jobs/{live.id}/retry"' in r.text
        assert f'action="/jobs/{archived.id}/retry"' in r.text
    finally:
        _remove_job(live)


def test_archived_job_page_keeps_retry_and_undo(client, monkeypatch):
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()

    failed = jm.Job(title="Archived album", artist="Portishead",
                    album_id="album-id")
    failed.status = jm.JobStatus.FAILED
    failed.finished_at = time.time()
    job_persistence.persist(failed)

    single = jm.Job(title="Archived single", artist="Portishead")
    single.status = jm.JobStatus.DONE
    single.single = {
        "dir": "/music/Portishead/Dummy", "track_id": "t1",
        "owned_path": {
            "relative": "01 - Track.flac",
            "directories": [[1, 2]],
            "file": {
                "device": 1, "inode": 3, "size": 4,
                "modified_ns": 5, "changed_ns": 6,
            },
        },
    }
    single.finished_at = time.time()
    job_persistence.persist(single)

    r = client.get(f"/jobs/{failed.id}")
    assert r.status_code == 200
    assert "This job is archived." in r.text
    assert ">Retry</button>" in r.text

    r = client.get(f"/jobs/{single.id}")
    assert r.status_code == 200
    assert f'action="/jobs/{single.id}/undo"' in r.text


def test_retry_rebuilds_archived_failed_download(client, monkeypatch):
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()

    archived = jm.Job(title="Dummy", artist="Portishead", album_id="al1")
    archived.status = jm.JobStatus.FAILED
    archived.finished_at = time.time() - 10
    job_persistence.persist(archived)

    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(
        "qobuz_librarian.api.search.get_album",
        lambda album_id, token: {"title": "Dummy",
                                 "artist": {"name": "Portishead"},
                                 "tracks": {"items": []}})
    monkeypatch.setattr(webapp, "_make_download_run",
                        lambda album, token, treat_as_new=False: lambda j: None)

    r = client.post(f"/jobs/{archived.id}/retry", follow_redirects=False)

    assert r.status_code == 303
    new_id = r.headers["location"].removeprefix("/jobs/")
    assert new_id and new_id != archived.id
    new_job = jm.registry.get(new_id)
    assert new_job is not None and new_job.album_id == "al1"
    _remove_job(new_job)


def test_retry_keeps_the_new_edition_override(client, monkeypatch):
    # "Download this edition anyway" lives on the job (execute_args), not just
    # in the run closure — a retried edition download that lost the flag would
    # hit the owned-album skip and quietly do nothing.
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()

    archived = jm.Job(title="Dummy", artist="Portishead", album_id="al1")
    archived.execute_args = {"new_edition": True}
    archived.status = jm.JobStatus.FAILED
    archived.finished_at = time.time() - 10
    job_persistence.persist(archived)

    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(
        "qobuz_librarian.api.search.get_album",
        lambda album_id, token: {"title": "Dummy",
                                 "artist": {"name": "Portishead"},
                                 "tracks": {"items": []}})
    seen = {}

    def fake_run(album, token, *, treat_as_new=False):
        seen["treat_as_new"] = treat_as_new
        return lambda j: None
    monkeypatch.setattr(webapp, "_make_download_run", fake_run)

    r = client.post(f"/jobs/{archived.id}/retry", follow_redirects=False)

    assert r.status_code == 303
    assert seen.get("treat_as_new") is True
    new_id = r.headers["location"].removeprefix("/jobs/")
    new_job = jm.registry.get(new_id)
    assert new_job is not None
    assert (new_job.execute_args or {}).get("new_edition") is True
    _remove_job(new_job)


def test_undo_burns_the_one_shot_in_the_archive(client, monkeypatch, tmp_path):
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()

    gone = tmp_path / "Portishead" / "Dummy"
    job = jm.Job(title="Dummy", artist="Portishead")
    job.status = jm.JobStatus.DONE
    job.single = {"dir": str(gone), "track_id": "t1", "title": "Glory Box"}
    job.finished_at = time.time()
    job_persistence.persist(job)

    r = client.post(f"/jobs/{job.id}/undo", follow_redirects=False)

    assert r.status_code == 303
    row = job_persistence.load_one(job.id)
    assert row is not None
    assert row["single"].get("removed") is True


def test_undo_bounces_when_the_staging_mutex_is_held(client, monkeypatch, tmp_path):
    """Undo behind a long staging-lock holder (library-wide Lyrics scan,
    migration) must bounce naming the holder instead of hanging the request
    until the holder finishes — the DONE job page can't show progress, so a
    blocking wait is invisible. The timer below is a watchdog: without the fix
    the request blocks on the held lock, the timer releases it, the undo runs
    to completion and the 503 assert fails instead of the test hanging."""
    import threading

    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()

    gone = tmp_path / "Portishead" / "Dummy"
    job = jm.Job(title="Dummy", artist="Portishead")
    job.status = jm.JobStatus.DONE
    job.single = {"dir": str(gone), "track_id": "t1", "title": "Glory Box"}
    job.finished_at = time.time()
    job_persistence.persist(job)

    lock = jm.staging_lock()
    lock.acquire()
    jm.set_staging_holder("Lyrics scan")
    release_timer = threading.Timer(3.0, lock.release)
    release_timer.start()
    try:
        r = client.post(f"/jobs/{job.id}/undo", follow_redirects=False)

        assert r.status_code == 503
        assert "Lyrics scan" in r.text
        row = job_persistence.load_one(job.id)
        assert not row["single"].get("removed")
    finally:
        jm.set_staging_holder(None)
        release_timer.cancel()
        try:
            lock.release()
        except RuntimeError:
            pass


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


def test_library_hide_then_restore_round_trip(client, monkeypatch, tmp_path):
    """Dismissing an artist from a library review writes the durable store and
    drops those candidates; the Dismissed albums and Gap Fill view then restores them."""
    from qobuz_librarian.library import hidden
    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", tmp_path / "h.json")

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    c_dummy = job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                                payload={"year": "1994"}, selected=False)
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=False)
    job.add_candidate(kind="album", title="Untrue", artist="Burial",
                      payload={"year": "2007"}, selected=False)
    try:
        # Selection is server-backed: tick Dummy via the select endpoint, then
        # dismissing unselected Portishead albums drops only Third, keeps the
        # ticked Dummy, and never touches Burial.
        r = client.post(f"/jobs/{job.id}/select",
                        data={"cid": c_dummy, "checked": "1"})
        assert r.status_code == 200 and r.json()["selected"] == 1
        r = client.post(f"/jobs/{job.id}/hide", data={"artist": "Portishead"})
        assert r.status_code == 200
        survivors = {c["artist"] + "/" + c["title"]: c["selected"]
                     for c in job.candidates}
        assert survivors == {"Portishead/Dummy": True, "Burial/Untrue": False}
        store = hidden.load()
        assert hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Third", store)
        assert not hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Dummy", store)
        assert not hidden.is_hidden(hidden.SCOPE_MISSING, "Burial", "Untrue", store)

        r = client.get("/library/hidden")
        assert r.status_code == 200
        assert "Portishead" in r.text
        assert 'href="/?kind=artist&q=Portishead"' in r.text
        assert 'href="/artist?artist=Portishead"' not in r.text

        r = client.post("/library/hidden/restore", data={"artist": "Portishead"})
        assert r.status_code == 200  # follows the 303 to the dismissed-items view
        assert hidden.count(hidden.SCOPE_MISSING) == 0
    finally:
        _remove_job(job)


def test_library_hide_scoped_to_review_tab(client, monkeypatch, tmp_path):
    """A library review with both missing albums and Gap Fill splits into tabs,
    and dismissing an artist's unselected rows from one tab must not silently
    drop that artist's candidates on the other tab."""
    from qobuz_librarian.library import hidden
    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", tmp_path / "h.json")

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=False)
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      detail="1994 · CD 16-bit/44.1kHz · gap-fill: 2 missing of 11",
                      payload={"year": "1994", "gap_fill": 2}, selected=False)
    try:
        r = client.get(f"/jobs/{job.id}")
        assert r.status_code == 200
        assert "Missing Albums" in r.text and "Gap Fill" in r.text
        # The default tab shows only the missing album, not the gap fill row.
        assert "Third" in r.text and "Dummy" not in r.text
        r = client.get(f"/jobs/{job.id}/review", params={"tab": "gaps"},
                       headers={"HX-Request": "true"})
        assert "Dummy" in r.text and "Third" not in r.text

        r = client.post(f"/jobs/{job.id}/hide",
                        data={"artist": "Portishead", "tab": "missing"})
        assert r.status_code == 200
        assert [c["title"] for c in job.candidates] == ["Dummy"]
        store = hidden.load()
        assert hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Third", store)
        assert not hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Dummy", store)
    finally:
        _remove_job(job)


def test_library_approve_scoped_to_tab_splits_off_other_tab(client, monkeypatch):
    """Downloading from one tab must consume only that tab: the other tab's
    candidates (and their saved ticks) split into their own parked review
    instead of dying with the executing job."""
    from qobuz_librarian.web import app as webapp
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "t", "user_id": "u"})
    monkeypatch.setattr(webapp, "_TOKEN_VALID", True)
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
        assert split._execute_fn is not None
    finally:
        _remove_job(job)
        if split is not None:
            _remove_job(split)


def test_search_download_prunes_parked_library_review(client, monkeypatch, tmp_path):
    """A Search download that imports an album must drop that album from a
    parked library review — otherwise the stale review offers to download it
    again. Other candidates and their ticks stay put."""
    from qobuz_librarian.web import app as webapp
    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", tmp_path / "h.json")
    monkeypatch.setattr("qobuz_librarian.modes.process.process_album",
                        lambda *a, **k: {"imported": True, "n_ok": 9})

    parked = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    parked.execute_kind = "library"
    parked.add_candidate(kind="album", title="Third", artist="Portishead",
                         payload={"album_id": "q123", "year": "2008"},
                         selected=True)
    parked.add_candidate(kind="album", title="Dummy", artist="Portishead",
                         payload={"album_id": "q456", "year": "1994"},
                         selected=True)
    runner = _inject_job(jm.JobStatus.RUNNING)
    try:
        album = {"id": "q123", "title": "Third",
                 "artist": {"name": "Portishead"}}
        webapp._make_download_run(album, token="tok")(runner)
        assert runner.status != jm.JobStatus.FAILED
        flags = {c["title"]: c["selected"] for c in parked.candidates}
        assert flags == {"Dummy": True}
        assert parked.status == jm.JobStatus.AWAITING_REVIEW
    finally:
        _remove_job(parked)
        _remove_job(runner)


def test_library_approve_skips_candidates_already_on_disk(client, monkeypatch):
    """Approving a parked review re-checks the disk: a missing-album candidate
    whose folder appeared while the review sat parked is dropped (and counted
    in the redirect note) instead of downloaded again. Gap Fill candidates are
    exempt — their folder exists by definition."""
    from qobuz_librarian.web import app as webapp
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "t", "user_id": "u"})
    monkeypatch.setattr(webapp, "_TOKEN_VALID", True)
    monkeypatch.setattr(jm._scan_queue, "put", lambda item: None)
    monkeypatch.setattr(
        "qobuz_librarian.library.catalog.find_album_dir_filesystem",
        lambda alb: Path("/music/Portishead/Third") if alb.get("id") == "q123"
        else None)
    # The faked folder stands in for a real one, so treat it as holding audio.
    monkeypatch.setattr(
        "qobuz_librarian.library.catalog._count_audio_files_in", lambda d: 1)

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.review_verb = "Download"
    job._execute_fn = lambda j, chosen: None
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"album_id": "q123", "year": "2008"},
                      selected=True)
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      payload={"album_id": "q456", "year": "1994"},
                      selected=True)
    # An owned-looking gap candidate must survive the disk check.
    job.add_candidate(kind="album", title="Roseland NYC Live",
                      artist="Portishead",
                      payload={"album_id": "q123", "gap_fill": 2},
                      selected=False)
    split = None
    try:
        r = client.post(f"/jobs/{job.id}/approve", data={"tab": "missing"},
                        follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/library?approved=1&skipped=1"
        assert [c["title"] for c in job.candidates] == ["Dummy"]
        split = next(j for j in jm.registry.all()
                     if j is not job and j.execute_kind == "library"
                     and j.status == jm.JobStatus.AWAITING_REVIEW)
        assert [c["title"] for c in split.candidates] == ["Roseland NYC Live"]
        # The note is rendered on /library.
        r = client.get("/library?approved=1&skipped=1")
        assert "1 album already in your library — skipped." in r.text
    finally:
        _remove_job(job)
        if split is not None:
            _remove_job(split)


def test_library_approve_when_everything_is_already_on_disk(client, monkeypatch):
    from qobuz_librarian.web import app as webapp
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "t", "user_id": "u"})
    monkeypatch.setattr(webapp, "_TOKEN_VALID", True)
    monkeypatch.setattr(jm._scan_queue, "put", lambda item: None)
    monkeypatch.setattr(
        "qobuz_librarian.library.catalog.find_album_dir_filesystem",
        lambda alb: Path("/music/Portishead/x"))
    monkeypatch.setattr(
        "qobuz_librarian.library.catalog._count_audio_files_in", lambda d: 1)
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job._execute_fn = lambda j, chosen: None
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"album_id": "q123"}, selected=True)
    try:
        r = client.post(f"/jobs/{job.id}/approve", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/library?skipped=1"
        # Nothing left to review or download; the review completed quietly.
        assert job.candidates == []
        assert job.status != jm.JobStatus.AWAITING_REVIEW
    finally:
        _remove_job(job)


def test_drop_owned_keeps_a_missing_album_whose_folder_is_an_empty_shell(
        tmp_path, monkeypatch):
    """A fully-missing candidate whose only on-disk match is an empty folder —
    a failed download or deleted tracks that left the directory behind — stays
    in the review. A name-matching shell with no audio isn't ownership, and the
    scanner still lists that album missing; dropping it would hide a real gap."""
    from qobuz_librarian.web import flows

    shell = tmp_path / "Runnin' Wild (2019)"
    shell.mkdir()
    real = tmp_path / "Real Album (2010)"
    real.mkdir()
    (real / "01 - track.flac").write_bytes(b"\x00")

    monkeypatch.setattr(
        "qobuz_librarian.library.catalog.find_album_dir_filesystem",
        lambda alb: shell if alb.get("id") == "empty1"
        else real if alb.get("id") == "real1" else None)

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.add_candidate(kind="album", title="Runnin' Wild", artist="Airbourne",
                      payload={"album_id": "empty1"}, selected=True)
    job.add_candidate(kind="album", title="Real Album", artist="Airbourne",
                      payload={"album_id": "real1"}, selected=True)
    try:
        dropped = flows.drop_owned_missing_candidates(job)
        titles = [c["title"] for c in job.candidates]
        assert "Runnin' Wild" in titles
        assert "Real Album" not in titles
        assert dropped == 1
    finally:
        _remove_job(job)


def test_library_select_all_scoped_to_tab(client):
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=False)
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      payload={"year": "1994", "gap_fill": 2}, selected=False)
    try:
        r = client.post(f"/jobs/{job.id}/select-all",
                        data={"on": "1", "scope": "all", "tab": "missing"})
        assert r.status_code == 200
        c = r.json()
        assert (c["missing_selected"], c["gap_selected"]) == (1, 0)
        flags = {x["title"]: x["selected"] for x in job.candidates}
        assert flags == {"Third": True, "Dummy": False}
    finally:
        _remove_job(job)


def test_select_all_scoped_to_the_active_filter(client):
    """With a filter showing 3 rows, Select all must not silently flip
    the other thousand — and Deselect must scope the same way so a filtered
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
        flags = {x["title"]: x["selected"] for x in job.candidates}
        assert flags == {"Third": False, "Ashes": True}
        # Empty query keeps the whole-tab behavior.
        client.post(f"/jobs/{job.id}/select-all",
                    data={"on": "1", "scope": "all", "tab": "missing", "q": ""})
        flags = {x["title"]: x["selected"] for x in job.candidates}
        assert flags == {"Third": True, "Ashes": True}
    finally:
        _remove_job(job)


def test_dismiss_rest_scoped_to_the_active_filter(client):
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job._execute_fn = lambda j, chosen: None
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=False)
    job.add_candidate(kind="album", title="Ashes", artist="Agalloch",
                      payload={"year": "2006"}, selected=False)
    try:
        r = client.post(f"/jobs/{job.id}/dismiss-rest",
                        data={"tab": "missing", "q": "agalloch"})
        assert r.status_code == 200
        assert r.json()["hidden"] == 1
        titles = [c["title"] for c in job.candidates]
        assert titles == ["Third"]
    finally:
        from qobuz_librarian.library import hidden as hidden_mod
        hidden_mod.restore(hidden_mod.SCOPE_MISSING, ["Agalloch"])
        _remove_job(job)


def test_review_pages_split_on_a_candidate_budget():
    """Pagination counts candidates, not just artists — a few prolific
    artists must not put thousands of rows in one page's DOM. Whole groups
    stay together; a single over-budget group still gets its own page."""
    from qobuz_librarian.web import app as webapp

    big = [("Artist %d" % i, [{"cid": f"c{i}-{j}"} for j in range(900)])
           for i in range(4)]
    page1, page, n_pages = webapp._paginate_groups(big, 1)
    assert n_pages == 4  # 900+900 > 1500, so one group per page
    assert [a for a, _ in page1] == ["Artist 0"]
    monster = [("Huge", [{"cid": f"c{j}"} for j in range(3000)]),
               ("Small", [{"cid": "s1"}])]
    p1, _, n = webapp._paginate_groups(monster, 1)
    assert n == 2 and [a for a, _ in p1] == ["Huge"]


def test_mangled_query_param_renders_the_error_page(client):
    """A mangled page param (/library?page=abc) must answer with the styled
    error page, not raw
    framework validation JSON; API routes keep the JSON detail."""
    r = client.get("/library?page=abc")
    assert r.status_code == 400
    assert "text/html" in r.headers.get("content-type", "")
    assert "Bad request" in r.text
    r2 = client.get("/api/jobs?status=nonsense")
    assert "application/json" in r2.headers.get("content-type", "")


def test_review_zero_selection_has_clear_disabled_action(client):
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.review_verb = "Download"
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      payload={"year": "1994"}, selected=False)
    try:
        r = client.get(f"/jobs/{job.id}")
        assert r.status_code == 200
        assert "Select candidates to download" in r.text
        assert "Download 1 selected" not in r.text
    finally:
        _remove_job(job)


def test_review_artist_header_carries_group_controls(client):
    # The select-artist checkbox lives in the group header (its own activation
    # runs instead of the summary's); the dismiss button must stay OUTSIDE the
    # summary, where a click can't fold the group.
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.review_verb = "Download"
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      payload={"year": "1994"}, selected=True)
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=False)
    try:
        r = client.get(f"/jobs/{job.id}")

        assert r.status_code == 200
        summary = r.text.split('<summary class="ql-review-summary">', 1)[1].split("</summary>", 1)[0]
        assert 'data-artist-select value="Portishead"' in summary
        assert "data-hide" not in summary
        assert 'data-hide data-artist="Portishead"' in r.text
    finally:
        _remove_job(job)


def test_review_hides_page_select_when_everything_is_on_one_page(client):
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.review_verb = "Download"
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      payload={"year": "1994"}, selected=True)
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=False)
    try:
        r = client.get(f"/jobs/{job.id}")

        assert r.status_code == 200
        assert 'data-select-all="1"' in r.text
        assert "data-select-page" not in r.text
    finally:
        _remove_job(job)


def test_library_dismiss_rest_hides_everything_unselected(client, monkeypatch, tmp_path):
    from qobuz_librarian.library import hidden
    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", tmp_path / "h.json")

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
    finally:
        _remove_job(job)


def test_sse_done_event_carries_final_status(client):
    """The done event reports the job's real terminal status so the page can
    flip the badge to failed/canceled instead of assuming success."""
    job = jm.Job(title="failed-job")
    job.status = jm.JobStatus.FAILED
    jm.registry.add(job)
    try:
        with client.stream("GET", f"/api/jobs/{job.id}/stream") as r:
            assert r.status_code == 200
            seen = ""
            for chunk in r.iter_text():
                seen += chunk
                if "event: done" in seen:
                    break
            else:
                pytest.fail("SSE stream never sent 'event: done'")
        assert "data: failed" in seen
    finally:
        _remove_job(job)


def test_persistence_restores_awaiting_review_with_candidates(monkeypatch):
    """The headline reliability win: a completed scan's candidates survive a
    container restart — the user can still approve them instead of re-scanning
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

    # And the user can still approve — the execute_fn was rebound from the
    # kind registry rather than vanishing with the dead closure.
    jm.start_worker()
    assert jm.approve(restored, ["c0"]) is True
    assert _wait_for(lambda: restored.status == jm.JobStatus.DONE)
    assert executed.get("ids") == ["abc"]


def test_rehydrated_review_never_mints_colliding_cids(monkeypatch):
    """A job rebuilt with pre-existing candidates (restart, tab split) must
    advance its cid counter past them — a fresh c0/c1 colliding with inherited
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
        # A legacy row persisted before seq existed — recovered from the cid.
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


def test_library_download_parks_unselected_and_keeps_only_picks():
    """A partial approval downloads only the picks and preserves the rest."""
    from qobuz_librarian.web import app as webapp

    job = jm.Job(title="Library scan")
    job.kind = "scan"
    job.execute_kind = "library"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job.add_candidate("album", "Picked", "X", payload={}, selected=True)
    job.add_candidate("album", "Unpicked", "X", payload={}, selected=False)
    job.add_candidate("gap", "OtherTab", "Y", payload={"gap_fill": True})
    other = None
    try:
        # Missing tab active: only the ticked "Picked" downloads; the unticked
        # missing album AND the whole Gap Fill tab stay parked.
        with job._lock:
            other = webapp._build_unapproved_review(job, "missing")
        assert other is not None
        kept = {c["title"] for c in job.candidates}
        parked = {c["title"] for c in other.candidates}
        assert kept == {"Picked"}
        assert parked == {"Unpicked", "OtherTab"}
        assert other.status == jm.JobStatus.AWAITING_REVIEW
        assert other.execute_kind == "library"
    finally:
        _remove_job(job)
        if other is not None:
            _remove_job(other)


def test_library_review_rebuilds_from_saved_state_when_no_live_job():
    """F1: with the baseline complete but no live library job (swept cancel,
    discarded scan job, corrupt restart row), the Missing Albums / Gap Fill
    review must rebuild from saved scan state — never 'Baseline ready' + no
    tabs. Retiring the review (discard / worked-through) blocks the rebuild."""
    from qobuz_librarian.library import library_scan_state
    from qobuz_librarian.web import app as webapp

    library_scan_state.save_kind("missing", artists={
        "Agalloch": {"fingerprint": "fp", "artist_id": "a1", "catalog_ids": [],
                     "candidates": [
            {"kind": "album", "title": "The Mantle", "artist": "Agalloch",
             "detail": "2002 · fully missing", "payload": {"album_id": "m1"}},
            {"kind": "album", "title": "Ashes", "artist": "Agalloch",
             "detail": "gap-fill: 2 missing",
             "payload": {"album_id": "m2", "gap_fill": 2}},
        ]},
    }, complete=True)
    job = None
    try:
        job = webapp._review_job_from_library_state()
        assert job is not None
        assert job.execute_kind == "library"
        assert job.status == jm.JobStatus.AWAITING_REVIEW
        assert {c["title"] for c in job.candidates} == {"The Mantle", "Ashes"}
        assert all(not c["selected"] for c in job.candidates)
        # Retire it (as a discard / empty would) → no rebuild from stale state.
        _remove_job(job)
        job = None
        library_scan_state.mark_review_retired(now=time.time() + 60)
        assert webapp._review_job_from_library_state() is None
    finally:
        library_scan_state.mark_review_retired(now=0)
        library_scan_state.save_kind("missing", artists={}, complete=False)
        if job is not None:
            _remove_job(job)


def test_partial_scan_does_not_resurrect_a_retired_library_review():
    """A partial scan does not revive a discarded Library review."""
    from qobuz_librarian.library import library_scan_state as lss
    from qobuz_librarian.web import app as webapp

    original = lss.load()
    try:
        # missing scanned at t0, review discarded at t1 > t0, then a later
        # gap-fill scan bumps the GLOBAL stamp to t2 > t1 (missing stays t0).
        lss._write_state({
            "version": lss.STATE_VERSION,
            "updated_at": 3000.0,            # bumped by the gap-fill save
            "review_retired_at": 2000.0,     # discard, after the missing scan
            "kinds": {
                "missing": {
                    "updated_at": 1000.0,    # missing kind's own stamp
                    "complete": True,
                    "hidden_signature": "", "quality_signature": "",
                    "artists": {"Agalloch": {
                        "fingerprint": "fp", "artist_id": "a1",
                        "catalog_ids": [], "candidates": [
                            {"kind": "album", "title": "The Mantle",
                             "artist": "Agalloch", "detail": "2002",
                             "payload": {"album_id": "m1"}}]}},
                },
                "gaps": {"updated_at": 3000.0, "complete": True,
                         "hidden_signature": "", "quality_signature": "",
                         "artists": {}},
            },
        })
        # Candidates are present, so a rebuild WOULD produce a job — proving the
        # None is the retirement block holding, not an empty candidate list.
        assert webapp._review_job_from_library_state() is None
    finally:
        lss._write_state(original)


def test_bring_back_lifts_a_retired_library_review():
    """Bring all back rebuilds a retired review from its saved state."""
    from qobuz_librarian.library import library_scan_state as lss
    from qobuz_librarian.web import app as webapp

    original = lss.load()
    try:
        lss.save_kind("missing", artists={
            "Agalloch": {"fingerprint": "fp", "artist_id": "a1", "catalog_ids": [],
                         "candidates": [
                {"kind": "album", "title": "The Mantle", "artist": "Agalloch",
                 "detail": "2002", "payload": {"album_id": "m1"}}]},
        }, complete=True)
        lss.mark_review_retired(now=time.time() + 60, reason="discarded")
        assert webapp._review_job_from_library_state() is None

        assert lss.clear_review_retired() is True
        job = webapp._review_job_from_library_state()
        assert job is not None
        assert {c["title"] for c in job.candidates} == {"The Mantle"}
        _remove_job(job)
        # Idempotent — nothing left to lift once it's cleared.
        assert lss.clear_review_retired() is False
    finally:
        lss._write_state(original)


def test_cancel_folds_unrun_picks_back_into_the_review():
    """Cancellation returns unstarted picks to the living review."""
    from qobuz_librarian.web import flows

    parked = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
    parked.execute_kind = "library"
    parked.add_candidate(kind="album", title="Left Unticked", artist="Agalloch",
                         payload={"album_id": "u1"}, selected=False)
    try:
        unrun = [
            {"cid": "c9", "seq": 9, "kind": "album", "title": "Unrun One",
             "artist": "Abigail", "detail": "", "payload": {"album_id": "r1"},
             "selected": True},
            {"cid": "c10", "seq": 10, "kind": "album", "title": "Unrun Two",
             "artist": "Abigail", "detail": "", "payload": {"album_id": "r2"},
             "selected": True},
        ]
        assert flows.refold_into_living_review(unrun) == 2
        by_title = {c["title"]: c for c in parked.candidates}
        assert {"Unrun One", "Unrun Two"} <= set(by_title)
        # They rejoin ticked; the existing leftover is untouched.
        assert by_title["Unrun One"]["selected"] is True
        assert by_title["Unrun Two"]["selected"] is True
        assert by_title["Left Unticked"]["selected"] is False
    finally:
        _remove_job(parked)


def test_cancel_mid_download_folds_the_in_flight_pick_too(monkeypatch):
    """Cancellation preserves both the in-flight and unstarted picks."""
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows

    parked = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
    parked.execute_kind = "library"
    running = _inject_job(jm.JobStatus.RUNNING, "Library scan")
    running.execute_kind = "library"
    running.add_candidate(kind="album", title="In Flight", artist="Abigail",
                          payload={"album_id": "r1"}, selected=True)
    running.add_candidate(kind="album", title="Never Started", artist="Abigail",
                          payload={"album_id": "r2"}, selected=True)
    chosen = list(running.candidates)
    monkeypatch.setattr(flows.cfg, "ARTIST_API_DELAY", 0)
    monkeypatch.setattr(flows, "get_album", lambda aid, _t: {"id": aid})
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)

    def fake_process(_full, *_a, **_k):
        # The first (and only reached) album is mid-download when the cancel lands.
        running.cancel_requested = True
        return {"result": "cancelled"}

    monkeypatch.setattr(process_mod, "process_album", fake_process)
    try:
        flows.execute_albums(running, chosen, "tok")
        by_title = {c["title"]: c for c in parked.candidates}
        # Both the in-flight album AND the never-started one rejoin, ticked.
        assert by_title.get("In Flight", {}).get("selected") is True
        assert by_title.get("Never Started", {}).get("selected") is True
    finally:
        _remove_job(parked)
        _remove_job(running)


def test_whole_review_download_retires_and_reparks_failures(monkeypatch, tmp_path):
    """A whole review retires successes and re-parks failures for retry."""
    from qobuz_librarian.library import library_scan_state as lss
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows

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


def test_reparked_failures_resolve_the_token_at_approve_time(monkeypatch, tmp_path):
    """The retry review parked for failed downloads must look the Qobuz token
    up when it is APPROVED, not reuse the value from the run that failed —
    otherwise a token replaced in Settings never reaches it and every retry
    fails with the same 'update it in Settings' error until a restart."""
    from qobuz_librarian.library import library_scan_state as lss
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows

    original = lss.load()
    running = _inject_job(jm.JobStatus.RUNNING, "Library scan")
    running.execute_kind = "library"
    running._consumed_whole_review = True
    running.add_candidate(kind="album", title="Failed One", artist="Agalloch",
                          payload={"album_id": "fail1"}, selected=True)
    chosen = list(running.candidates)
    monkeypatch.setattr(flows.cfg, "ARTIST_API_DELAY", 0)
    monkeypatch.setattr(flows, "get_album", lambda aid, _t: {"id": aid})
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "_refresh_after_local_album_change",
                        lambda *a, **k: None)
    monkeypatch.setattr(flows, "prune_library_review_candidates", lambda *a, **k: 0)
    monkeypatch.setattr(process_mod, "process_album",
                        lambda *a, **k: {"result": "error", "imported": False,
                                         "n_ok": 0})
    parked = None
    try:
        flows.execute_albums(running, chosen, "stale-tok")
        parked = next(j for j in jm.registry.awaiting_review()
                      if getattr(j, "execute_kind", "") == "library"
                      and any((c.get("payload") or {}).get("album_id") == "fail1"
                              for c in j.candidates))
        # Token replaced in Settings after the failed run; the retry must use it.
        monkeypatch.setattr(flows, "load_qobuz_token",
                            lambda: ("uid", "fresh-tok"))
        seen = {}
        monkeypatch.setattr(flows, "execute_albums",
                            lambda j, ch, token: seen.setdefault("token", token))
        parked._execute_fn(parked, list(parked.candidates))
        assert seen["token"] == "fresh-tok"
    finally:
        lss._write_state(original)
        _remove_job(running)
        if parked is not None:
            _remove_job(parked)


def test_partial_run_failure_folds_back_into_the_living_review(monkeypatch, tmp_path):
    """On a partial approve (only some picks ticked), a living split-off
    review still holds the unticked picks. An album that FAILS on that run must
    fold back into it, ticked, to retry — matching the whole-review re-park —
    instead of surviving only as the job's error line until a manual refresh."""
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows

    parked = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
    parked.execute_kind = "library"
    parked.add_candidate(kind="album", title="Left Unticked", artist="Agalloch",
                         payload={"album_id": "u1"}, selected=False)
    running = _inject_job(jm.JobStatus.RUNNING, "Library scan")
    running.execute_kind = "library"
    running._consumed_whole_review = False   # partial approve — the remnant lives
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
    try:
        flows.execute_albums(running, chosen, "tok")
        by_title = {c["title"]: c for c in parked.candidates}
        # The failure rejoined the living review, ticked; the leftover is untouched
        # and the successful download was NOT parked.
        assert by_title.get("Failed One", {}).get("selected") is True
        assert by_title["Left Unticked"]["selected"] is False
        assert "Downloaded OK" not in by_title
    finally:
        _remove_job(running)
        _remove_job(parked)


def test_new_release_run_recoveries_never_touch_the_library_review(monkeypatch):
    """A failed or cancelled NEW-RELEASE download run must not fold its albums
    into the parked Library review — new-release results never enter the
    Library tabs. Guards both fold-back call sites in execute_albums, which
    also runs new-release batches."""
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows

    parked = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
    parked.execute_kind = "library"
    parked.add_candidate(kind="album", title="Left Unticked", artist="Agalloch",
                         payload={"album_id": "u1"}, selected=False)
    monkeypatch.setattr(flows.cfg, "ARTIST_API_DELAY", 0)
    monkeypatch.setattr(flows, "get_album", lambda aid, _t: {"id": aid})
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "_refresh_after_local_album_change",
                        lambda *a, **k: None)
    monkeypatch.setattr(flows, "prune_library_review_candidates", lambda *a, **k: 0)
    try:
        # A run where an album fails outright.
        failing = _inject_job(jm.JobStatus.RUNNING, "New-release check")
        failing.execute_kind = "new_releases"
        failing.add_candidate(kind="album", title="NR Failed", artist="Agalloch",
                              payload={"album_id": "nr1"}, selected=True)
        monkeypatch.setattr(process_mod, "process_album",
                            lambda full, *_a, **_k: {"result": "error",
                                                     "imported": False, "n_ok": 0})
        try:
            flows.execute_albums(failing, list(failing.candidates), "tok")
        finally:
            _remove_job(failing)

        # A run cancelled mid-batch with a pick it never reached.
        cancelled = _inject_job(jm.JobStatus.RUNNING, "New-release check")
        cancelled.execute_kind = "new_releases"
        cancelled.add_candidate(kind="album", title="NR In Flight", artist="Agalloch",
                                payload={"album_id": "nr2"}, selected=True)
        cancelled.add_candidate(kind="album", title="NR Unreached", artist="Agalloch",
                                payload={"album_id": "nr3"}, selected=True)

        def cancelling(full, *_a, **_k):
            cancelled.cancel_requested = True
            return {"result": "cancelled", "imported": False, "n_ok": 0}

        monkeypatch.setattr(process_mod, "process_album", cancelling)
        try:
            flows.execute_albums(cancelled, list(cancelled.candidates), "tok")
        finally:
            _remove_job(cancelled)

        assert [c["title"] for c in parked.candidates] == ["Left Unticked"]
    finally:
        _remove_job(parked)


def test_auth_death_mid_batch_folds_unfinished_picks_back(monkeypatch, tmp_path):
    """A token death / Qobuz outage AFTER the first import fails the job
    (approve's no-harm re-park only covers the nothing-landed case), so on a
    partial approve the picks the run never finished must fold back into the
    living split-off review, ticked. Before anything lands the fold must NOT
    fire — approve() restores the whole review instead, and folding here too
    would offer the same picks twice."""
    from qobuz_librarian.api.auth import AuthLost
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows

    parked = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
    parked.execute_kind = "library"
    parked.add_candidate(kind="album", title="Left Unticked", artist="Agalloch",
                         payload={"album_id": "u1"}, selected=False)
    running = _inject_job(jm.JobStatus.RUNNING, "Library scan")
    running.execute_kind = "library"
    running._consumed_whole_review = False
    for title, aid in (("Landed", "ok1"), ("Died Mid-Rip", "die1"),
                       ("Never Started", "ns1")):
        running.add_candidate(kind="album", title=title, artist="Agalloch",
                              payload={"album_id": aid}, selected=True)
    chosen = list(running.candidates)
    monkeypatch.setattr(flows.cfg, "ARTIST_API_DELAY", 0)
    monkeypatch.setattr(flows, "get_album", lambda aid, _t: {"id": aid})
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "_refresh_after_local_album_change",
                        lambda *a, **k: None)
    monkeypatch.setattr(flows, "prune_library_review_candidates", lambda *a, **k: 0)

    def fake_process(full, *_a, **_k):
        if full["id"] == "die1":
            raise AuthLost("token expired")
        return {"imported": True, "n_ok": 1, "n_fail": 0, "result": "downloaded",
                "dir": str(tmp_path)}

    monkeypatch.setattr(process_mod, "process_album", fake_process)
    try:
        with pytest.raises(AuthLost):
            flows.execute_albums(running, chosen, "tok")
        by_title = {c["title"]: c for c in parked.candidates}
        # The album that died and the one never reached rejoin ticked; what
        # landed stays out; the untouched leftover keeps its state.
        assert by_title.get("Died Mid-Rip", {}).get("selected") is True
        assert by_title.get("Never Started", {}).get("selected") is True
        assert by_title["Left Unticked"]["selected"] is False
        assert "Landed" not in by_title
    finally:
        _remove_job(running)
        _remove_job(parked)

    # Nothing landed: the no-harm re-park recovers the whole job, so the fold
    # must stay out of it.
    parked = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
    parked.execute_kind = "library"
    parked.add_candidate(kind="album", title="Left Unticked", artist="Agalloch",
                         payload={"album_id": "u1"}, selected=False)
    running = _inject_job(jm.JobStatus.RUNNING, "Library scan")
    running.execute_kind = "library"
    running._consumed_whole_review = False
    running.add_candidate(kind="album", title="Died First", artist="Agalloch",
                          payload={"album_id": "die1"}, selected=True)
    try:
        with pytest.raises(AuthLost):
            flows.execute_albums(running, list(running.candidates), "tok")
        assert [c["title"] for c in parked.candidates] == ["Left Unticked"]
    finally:
        _remove_job(running)
        _remove_job(parked)


def test_bulk_cancel_pending_never_touches_parked_reviews():
    """Bulk cancellation leaves parked reviews untouched."""
    from qobuz_librarian.web import app as webapp

    review = jm.Job(title="Library scan")
    review.execute_kind = "library"
    review.status = jm.JobStatus.AWAITING_REVIEW
    review.add_candidate("album", "Keep me", "X", payload={})
    queued = jm.Job(title="Album", artist="A", album_id="q1")
    queued.status = jm.JobStatus.PENDING
    jm.registry.add(review)
    jm.registry.add(queued)
    try:
        asyncio.run(webapp.queue_cancel_pending())
        assert review.status == jm.JobStatus.AWAITING_REVIEW
        assert len(review.candidates) == 1
        assert queued.cancel_requested is True
    finally:
        _remove_job(review)
        _remove_job(queued)


def test_restart_interrupt_message_matches_the_retry_affordance(monkeypatch):
    # A job rebadged FAILED by a restart must not tell the user to "submit
    # this job again" unless it actually offers a Retry button.
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    album = jm.Job(title="Album", artist="Artist", album_id="abc")
    album.status = jm.JobStatus.RUNNING
    job_persistence.persist(album)

    lyrics = jm.Job(title="Lyrics backfill")
    lyrics.execute_kind = "lyrics"
    lyrics.status = jm.JobStatus.RUNNING
    job_persistence.persist(lyrics)

    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    jm.restore_jobs({})

    a = jm.registry.get(album.id)
    ly = jm.registry.get(lyrics.id)
    assert a.status == jm.JobStatus.FAILED and ly.status == jm.JobStatus.FAILED
    assert "Retry" in a.error
    assert "Submit this" not in ly.error
    assert "Lyrics" in ly.error


def test_interrupted_scan_summary_matches_the_real_resume_path(monkeypatch):
    """Only a pre-baseline library scan auto-resumes; post-baseline the
    summary must point at the manual resume notice instead."""
    from qobuz_librarian.web import job_persistence

    for complete, expect in ((False, "next time you open the app"),
                             (True, "notice on the Search page")):
        job_persistence._reset_for_tests()
        monkeypatch.setattr(job_persistence, "_disabled", False)
        job_persistence.init()
        monkeypatch.setattr(
            "qobuz_librarian.library.new_releases.is_baseline_complete",
            lambda complete=complete: complete)
        scan = jm.Job(title="Library scan")
        scan.execute_kind = "library"
        scan.status = jm.JobStatus.SCANNING
        job_persistence.persist(scan)
        monkeypatch.setattr(jm, "registry", jm.JobRegistry())
        jm.restore_jobs({})
        restored = jm.registry.get(scan.id)
        assert expect in restored.summary


def test_clear_history_serializes_with_durable_resume_publication(
        client, monkeypatch):
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()
    owner = jm.Job(id="resume-race", title="In progress", album_id="album-1")
    owner.status = jm.JobStatus.RUNNING
    jm.registry.add(owner)
    monkeypatch.setattr(
        webapp,
        "_STARTUP_RECOVERY_RESULT",
        StartupRecoveryResult(StartupRecoveryStatus.CLEAR),
    )
    monkeypatch.setattr(webapp, "_STARTUP_RECOVERY_UNKNOWN", False)
    jm.set_durable_recovery_job_id(None)

    clear_entered = threading.Event()
    publication_attempted = threading.Event()
    real_clear_finished = jm.registry.clear_finished

    def pause_clear_finished():
        clear_entered.set()
        assert publication_attempted.wait(2)
        real_clear_finished()

    monkeypatch.setattr(jm.registry, "clear_finished", pause_clear_finished)

    def publish_resume():
        assert clear_entered.wait(2)
        publication_attempted.set()
        with webapp._STARTUP_RECOVERY_LOCK:
            webapp._STARTUP_RECOVERY_RESULT = StartupRecoveryResult(
                StartupRecoveryStatus.RESUME_REQUIRED)
            jm.set_durable_recovery_job_id(owner.id)
            with owner._lock:
                owner.status = jm.JobStatus.FAILED
                owner.finished_at = time.time()
            assert job_persistence.persist(owner)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        publication = executor.submit(publish_resume)
        response = client.post("/queue/clear", follow_redirects=False)
        publication.result(timeout=3)

    assert response.status_code == 303
    assert jm.durable_recovery_job_id() == owner.id
    assert jm.registry.get(owner.id) is owner
    assert job_persistence.load_one(owner.id) is not None


def test_persist_survives_non_json_candidate_payload(monkeypatch):
    """A stray non-JSON value in a candidate payload (a Path, say) must coerce
    to text at the write boundary, not raise TypeError — that escaped the
    sqlite guard, killed the worker, and lost the whole parked review."""
    from pathlib import Path

    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    job = jm.Job(title="Review")
    job.kind = "scan"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job.add_candidate("album", "Album", "Artist",
                      payload={"album_id": "A1", "dir": Path("/music/A/B")})
    job_persistence.persist(job)

    row = job_persistence.load_one(job.id)
    assert row is not None
    assert row["candidates"][0]["payload"]["album_id"] == "A1"


def test_download_partial_album_proceeds_to_gap_fill(client, monkeypatch):
    from pathlib import Path

    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as cat_mod
    import qobuz_librarian.modes.process as proc_mod
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    album = {"id": "gap1", "title": "Gappy", "artist": {"name": "A"},
             "tracks": {"items": [{"id": 1}, {"id": 2}, {"id": 3}]}}
    monkeypatch.setattr(search_mod, "get_album", lambda _i, _t: album)
    monkeypatch.setattr(cat_mod, "find_album_dir_filesystem",
                        lambda _a: Path("/music/A/Gappy"))
    monkeypatch.setattr(cat_mod, "find_existing_tracks",
                        lambda _a, **_kw: ([{"id": 1}], None))
    monkeypatch.setattr(cat_mod, "compute_missing",
                        lambda q, e: ([{"id": 2}, {"id": 3}], [{"id": 1}]))
    monkeypatch.setattr(proc_mod, "process_album",
                        lambda *a, **k: {"result": "downloaded",
                                         "imported": True, "n_fail": 0})

    jm.start_worker()
    r = client.post("/download", data={"album_id": "gap1"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "already complete" not in r.text.lower()
    new_jobs = [j for j in list(jm.registry._jobs.values())
                if getattr(j, "album_id", None) == "gap1"]
    assert len(new_jobs) == 1
    job = new_jobs[0]
    try:
        _wait_for(lambda: job.status in (jm.JobStatus.DONE, jm.JobStatus.FAILED))
    finally:
        _remove_job(job)


def test_settings_save_rejects_out_of_enum_quality(tmp_path, monkeypatch):
    import json

    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss
    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(ss, "_any_active_job", lambda: False)
    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 4)

    assert ss.save({"STREAMRIP_QUALITY": "99"})[0] is True
    on_disk = json.loads((tmp_path / "s.json").read_text())
    assert on_disk.get("STREAMRIP_QUALITY") != "99"
    # A valid value still persists.
    assert ss.save({"STREAMRIP_QUALITY": "2"})[0] is True
    assert json.loads((tmp_path / "s.json").read_text())["STREAMRIP_QUALITY"] == "2"


def test_settings_omits_cli_only_consolidation_setting(
        client, tmp_path, monkeypatch):
    import json

    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(app_mod, "_read_creds",
                        lambda: {"user_id": "u", "auth_token": "t"})
    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(ss, "_any_active_job", lambda: False)
    monkeypatch.setattr(cfg, "CONSOLIDATE", True)

    r = client.get("/settings")

    assert r.status_code == 200
    assert "Consolidate duplicate folders" not in r.text
    assert "CLI only" not in r.text
    assert "CONSOLIDATE" not in r.text

    data = {"form_complete": "1", "CONSOLIDATE": "1"}
    data.update({key: "" for key in ss.TEXT_KEYS})
    r = client.post("/settings/behavior", data=data, follow_redirects=False)

    assert r.status_code == 303
    assert "CONSOLIDATE" not in json.loads((tmp_path / "s.json").read_text())


def test_settings_renders_both_forms_and_mode_switch(client, monkeypatch):
    import qobuz_librarian.web.app as app_mod
    monkeypatch.setattr(app_mod, "_read_creds",
                        lambda: {"user_id": "u", "auth_token": "t"})
    r = client.get("/settings")

    assert r.status_code == 200
    assert 'data-toggle-password="auth_token"' in r.text
    assert ">Save &amp; connect</button>" in r.text
    assert "Switch to terminal mode" in r.text
    assert ">Save behaviour</button>" in r.text
    assert "CLI command" in r.text
    assert "CLI entrypoint" not in r.text
    assert "Paths currently in use" in r.text
    assert "QL_MUSIC_DIR" not in r.text
    assert "QL_STAGING_DIR" not in r.text
    assert "MUSIC_ROOT" not in r.text
    assert "STAGING_DIR" not in r.text


# ── CLI/web mode hand-off ───────────────────────────────────────────────────────


def test_web_pauses_new_writes_if_its_run_lock_is_displaced(monkeypatch):
    import qobuz_librarian.web.app as app_mod

    class LostAuthority:
        @staticmethod
        def intact():
            return False

    monkeypatch.setattr(app_mod, "_RUN_LOCK_HANDLE", LostAuthority())
    monkeypatch.setattr(app_mod, "_CLI_MODE", False)
    monkeypatch.setattr(app_mod, "_LOCK_BUSY_PID", None)
    monkeypatch.setattr(app_mod, "_LOCK_UNENFORCEABLE", False)
    monkeypatch.setattr(app_mod, "_unwritable_volumes", lambda: [])
    monkeypatch.setattr(app_mod, "_SHUTTING_DOWN", False)

    assert app_mod._web_writes_paused() is True


def test_shutdown_keeps_run_lock_until_workers_and_direct_writes_settle(
        monkeypatch):
    import threading

    import qobuz_librarian.web.app as app_mod

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
    monkeypatch.setattr(app_mod, "_RUN_LOCK_HANDLE", handle)

    operation = jm.begin_library_operation("Restore")
    assert operation is not None
    shutdown = threading.Thread(target=app_mod._shutdown_web_mutations)
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
    assert app_mod._RUN_LOCK_HANDLE is None


def test_parked_review_does_not_block_cli_handoff(client):
    """The staging race the handoff guards against needs a running worker; a
    review waiting on the user has none, and it can wait for weeks — refusing
    on it would make terminal mode unreachable."""
    import qobuz_librarian.web.app as app_mod
    review = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    review.execute_kind = "downsample"

    r = client.post("/settings/mode", data={"target": "cli"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?mode=cli"
    assert app_mod._CLI_MODE is True
    back = client.post("/settings/mode", data={"target": "web"},
                       follow_redirects=False)
    assert back.status_code == 303
    assert app_mod._CLI_MODE is False


def test_resuming_web_mode_restores_saved_jobs_before_unpausing(
        client, monkeypatch):
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian import run_lock

    class Lease:
        @staticmethod
        def intact():
            return True

    lease = Lease()
    restored_under = []

    def restore_jobs(factories, *, durable_recovery_clear):
        assert factories is app_mod._RESUME_EXECUTE
        assert durable_recovery_clear is True
        restored_under.append((app_mod._CLI_MODE, app_mod._RUN_LOCK_HANDLE))

    monkeypatch.setattr(run_lock, "acquire", lambda: lease)
    monkeypatch.setattr(jm, "restore_jobs", restore_jobs)
    monkeypatch.setattr(app_mod, "_CLI_MODE", True)
    monkeypatch.setattr(app_mod, "_JOBS_RESTORED", False)

    response = client.post(
        "/settings/mode",
        data={"target": "web"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?mode=web"
    assert restored_under == [(True, lease)]
    assert app_mod._CLI_MODE is False


def test_mode_handoff_to_cli_pauses_web_downloads(client, monkeypatch):
    import qobuz_librarian.web.app as app_mod
    # No active job (the registry is a shared singleton across tests).
    monkeypatch.setattr(app_mod.job_mgr.registry, "pending_and_running",
                        lambda: [])
    r = client.post("/settings/mode", data={"target": "cli"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings?mode=cli"
    assert app_mod._CLI_MODE is True
    # The banner shows everywhere, and download/scan endpoints are paused.
    assert "Terminal (CLI) mode" in client.get("/").text
    blocked = client.post("/download", data={"album_id": "123"},
                          follow_redirects=False)
    assert blocked.status_code == 503 and "Terminal (CLI) mode" in blocked.text
    # Resume restores web mode.
    back = client.post("/settings/mode", data={"target": "web"},
                       follow_redirects=False)
    assert back.status_code == 303 and back.headers["location"] == "/settings?mode=web"
    assert app_mod._CLI_MODE is False


# ── web/auth.py: optional login ────────────────────────────────────────────────


def _enable_auth(monkeypatch, tmp_path, *, configure=True):
    """Turn auth on for one test against an isolated credential file. Returns
    a client bound to the app. The session-wide conftest default of
    WEB_AUTH=none is restored on teardown by monkeypatch."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import app as app_mod
    from qobuz_librarian.web import auth as web_auth

    monkeypatch.setenv("WEB_AUTH", "")
    _run_web_executors_inline(monkeypatch, app_mod)
    monkeypatch.setattr(cfg, "WEB_AUTH_FILE", tmp_path / "web_auth.json")
    if configure:
        assert web_auth.set_credentials("admin", "hunter2hunter")
    return _SameThreadASGIClient(app_mod.app)


def test_login_rejects_wrong_password(monkeypatch, tmp_path):
    with _enable_auth(monkeypatch, tmp_path) as c:
        c.get("/login")
        tok = c.cookies.get("ql_csrf")
        r = c.post("/login",
                   data={"username": "admin", "password": "nope",
                         "_csrf_token": tok},
                   headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert r.status_code == 401
        assert "ql_session" not in r.cookies
        # Still locked out afterwards.
        assert c.get("/", follow_redirects=False).status_code == 303


def test_login_accepts_correct_password(monkeypatch, tmp_path):
    with _enable_auth(monkeypatch, tmp_path) as c:
        c.get("/login")
        tok = c.cookies.get("ql_csrf")
        r = c.post("/login",
                   data={"username": "admin", "password": "hunter2hunter",
                         "_csrf_token": tok},
                   headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        # The session cookie now opens a protected route.
        assert c.get("/", follow_redirects=False).status_code == 200


def test_authenticated_pages_are_not_stored_in_the_browser_cache(
        monkeypatch, tmp_path):
    with _enable_auth(monkeypatch, tmp_path) as c:
        c.get("/login")
        tok = c.cookies.get("ql_csrf")
        c.post(
            "/login",
            data={"username": "admin", "password": "hunter2hunter",
                  "_csrf_token": tok},
            headers={"X-CSRF-Token": tok},
            follow_redirects=False,
        )

        page = c.get("/settings")
        assert page.headers["cache-control"] == "no-store"

        jobs = c.get("/api/jobs")
        assert jobs.status_code == 200
        assert jobs.headers["cache-control"] == "no-store"

        logout_response = c.post(
            "/logout",
            data={"_csrf_token": tok},
            headers={"X-CSRF-Token": tok},
            follow_redirects=False,
        )
        assert logout_response.status_code == 303
        assert logout_response.headers["cache-control"] == "no-store"

        assert c.get("/sw.js").headers["cache-control"] == "no-cache"
        assert "no-store" not in c.get("/static/app.js").headers.get(
            "cache-control", ""
        )
        assert "no-store" not in c.get("/static/offline.html").headers.get(
            "cache-control", ""
        )


def test_login_returns_to_the_page_that_bounced(monkeypatch, tmp_path):
    # A deep link opened while logged out should survive the login bounce:
    # /queue → /login?next=/queue → sign in → land on /queue, not the dashboard.
    with _enable_auth(monkeypatch, tmp_path) as c:
        r = c.get("/queue", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login?next=/queue"
        r = c.get("/login?next=/queue")
        assert 'name="next" value="/queue"' in r.text
        tok = c.cookies.get("ql_csrf")
        r = c.post("/login",
                   data={"username": "admin", "password": "hunter2hunter",
                         "_csrf_token": tok, "next": "/queue"},
                   headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/queue"


def test_login_next_cannot_leave_the_app(monkeypatch, tmp_path):
    # The next field is attacker-writable (it rides links and the login form),
    # so anything that could land off-site or loop must fall back to "/".
    from qobuz_librarian.web import auth as web_auth

    for bad in ("//evil.example", "/\\evil.example", "https://evil.example",
                "javascript:alert(1)", "/login", "/setup", "/api/jobs",
                "/a\r\nSet-Cookie:x=1", "queue", ""):
        assert web_auth.safe_next_path(bad) == "", bad
    assert web_auth.safe_next_path("/queue") == "/queue"
    assert web_auth.safe_next_path("/jobs/abc?x=1") == "/jobs/abc?x=1"

    with _enable_auth(monkeypatch, tmp_path) as c:
        c.get("/login")
        tok = c.cookies.get("ql_csrf")
        r = c.post("/login",
                   data={"username": "admin", "password": "hunter2hunter",
                         "_csrf_token": tok, "next": "//evil.example"},
                   headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"


def test_malformed_host_cannot_bypass_auth(monkeypatch, tmp_path):
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


def test_artist_sort_key_files_articles_under_the_real_letter():
    # "The Beatles" must sort under B, not T (owner acceptance criterion);
    # leading the/a/an are ignored, the rest of the name is not.
    from qobuz_librarian.web.app import _artist_sort_key
    names = ["The Beatles", "Bob Dylan", "ABBA", "Adele", "The Who",
             "A Tribe Called Quest", "an Evening"]
    ordered = sorted(names, key=_artist_sort_key)
    assert ordered.index("Adele") < ordered.index("The Beatles") < ordered.index("Bob Dylan")
    assert ordered[-1] == "The Who"            # "who" sorts last
    assert _artist_sort_key("The Beatles") == "beatles"
    assert _artist_sort_key("A Tribe Called Quest") == "tribe called quest"
    assert _artist_sort_key("Adele") == "adele"   # no leading "a " to strip


def test_review_artist_groups_use_library_sort_order():
    from qobuz_librarian.web.app import _review_artist_groups

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.add_candidate(kind="album", title="Revolver", artist="The Beatles",
                      payload={"album_id": "beatles"})
    job.add_candidate(kind="album", title="Highway 61 Revisited",
                      artist="Bob Dylan", payload={"album_id": "dylan"})
    job.add_candidate(kind="album", title="Low", artist="David Bowie",
                      payload={"album_id": "bowie"})

    assert [artist for artist, _items in _review_artist_groups(job)] == [
        "The Beatles",
        "Bob Dylan",
        "David Bowie",
    ]


def test_migrate_post_submits_a_creds_free_job(client, monkeypatch, tmp_path):
    import qobuz_librarian.config as cfg
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setattr(cfg, "MIGRATE_SRC", str(src))
    monkeypatch.setattr(cfg, "MIGRATE_DEST", str(tmp_path / "dest"))
    r = client.post("/migrate", data={"in_place": "on"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/jobs/")
    job_id = r.headers["location"].split("/jobs/")[1].split("?")[0]
    job = jm.registry.get(job_id)
    assert job is not None
    assert job.review_verb == "Move"                  # in-place toggle carried through
    _remove_job(job)


def test_migrate_offers_a_preview(client, monkeypatch, tmp_path):
    import qobuz_librarian.config as cfg

    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setattr(cfg, "MIGRATE_SRC", str(src))
    monkeypatch.setattr(cfg, "MIGRATE_DEST", str(tmp_path / "dest"))

    r = client.get("/migrate")

    assert r.status_code == 200
    assert ">Preview migration</button>" in r.text


def test_settings_path_resolver_maps_container_paths_to_host_bind_mounts(
    monkeypatch, tmp_path
):
    from qobuz_librarian.web.app import _resolve_host_path

    fake_mountinfo = (
        "1 0 0:1 / / rw - overlay overlay rw\n"
        "2 1 0:2 /home/me/music /music rw - ext4 /dev/sda1 rw\n"
        "3 1 0:3 /home/me/stack/config /config rw - ext4 /dev/sda1 rw\n"
    )
    fake = tmp_path / "mountinfo"
    fake.write_text(fake_mountinfo)
    import builtins
    real_open = builtins.open
    def patched_open(path, *a, **kw):
        if path == "/proc/self/mountinfo":
            return real_open(fake, *a, **kw)
        return real_open(path, *a, **kw)
    monkeypatch.setattr(builtins, "open", patched_open)

    assert _resolve_host_path("/music") == ("/home/me/music", True)
    assert _resolve_host_path("/config/beets/musiclibrary.db") == (
        "/home/me/stack/config/beets/musiclibrary.db", True)
    assert _resolve_host_path("/anonymous-volume") == ("/anonymous-volume", False)
    from pathlib import Path
    assert _resolve_host_path(Path("/music")) == ("/home/me/music", True)


def test_session_tokens_are_per_login_and_revocable():
    from qobuz_librarian.web import auth as web_auth
    web_auth.revoke_all_sessions()
    t1 = web_auth.mint_session()
    t2 = web_auth.mint_session()
    assert t1 != t2                              # per-login, not one shared secret
    assert web_auth.verify_session(t1) and web_auth.verify_session(t2)
    web_auth.revoke_session(t1)                  # logout of one browser
    assert not web_auth.verify_session(t1)       # ...that session is dead...
    assert web_auth.verify_session(t2)           # ...the other still works
    web_auth.revoke_all_sessions()               # e.g. on a password change
    assert not web_auth.verify_session(t2)
    assert web_auth.verify_session("") is False


def test_restore_backup_rejects_path_shaped_names(client, tmp_path, monkeypatch):
    # The Restore form posts a bare directory name; anything path-shaped is a
    # probe, not a backup the diagnostics list rendered — it must not resolve
    # outside the backup dir or restore anything.
    from qobuz_librarian import config as cfg
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    (tmp_path / "backups").mkdir()
    r = client.post("/backups/restore", data={"backup": "../../etc"})
    assert r.status_code == 200
    assert "isn't there anymore" in r.text


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
        assert "Restored the album" in r.text
        assert job.recoveries == []
        assert job.attention == ""
        assert job_persistence.load_one(job.id)["recoveries"] == []
    finally:
        _remove_job(job)


def test_discard_backup_removes_a_redundant_backup(client, tmp_path, monkeypatch):
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
    origin.mkdir(parents=True)
    (origin / "01 - Song.flac").write_bytes(b"data")
    job = jm.Job(title="Repair needing recovery")
    job.execute_kind = "repair"
    job.status = jm.JobStatus.FAILED
    job.finished_at = time.time()
    job.attention = "recovery"
    job.recoveries = [_repair_recovery_record(carried.path, carried.receipt)]
    jm.registry.add(job)
    try:
        r = client.post("/backups/discard", data={"backup": carried.name})
        assert r.status_code == 200
        assert "Removed the backup" in r.text
        assert not carried.exists()
        assert (origin / "01 - Song.flac").read_bytes() == b"data"
        assert job.recoveries == []
        assert job.attention == ""

        # A backup whose origin copy differs must survive the same request.
        (origin / "02 - Other.flac").write_bytes(b"more")
        kept = backup_mod.backup_album_dir(origin)
        assert kept is not None and kept.complete is True
        origin.mkdir(parents=True)
        (origin / "01 - Song.flac").write_bytes(b"data")
        (origin / "02 - Other.flac").write_bytes(b"MORE")
        r = client.post("/backups/discard", data={"backup": kept.name})
        assert r.status_code == 200
        assert "byte-for-byte" in r.text
        assert kept.exists()
        assert (kept.path / "02 - Other.flac").read_bytes() == b"more"
    finally:
        _remove_job(job)


def test_auth_loss_fires_the_hook_once_per_transition(monkeypatch):
    # Only the healthy→rejected edge should push a notification; the 401s
    # that follow are the same outage, and a recovery re-arms it.
    from qobuz_librarian.web import app as app_mod
    calls = []
    monkeypatch.setattr(app_mod.job_mgr, "fire_auth_lost_hook",
                        lambda: calls.append(1))
    monkeypatch.setattr(app_mod, "_TOKEN_VALID", None)
    app_mod._on_auth_state(False)
    app_mod._on_auth_state(False)
    assert len(calls) == 1
    app_mod._on_auth_state(True)
    app_mod._on_auth_state(False)
    assert len(calls) == 2


def test_refresh_folds_into_parked_library_review(monkeypatch):
    from qobuz_librarian.web import app as webapp

    parked = jm.Job(title="Library scan")
    parked.execute_kind = "library"
    parked.add_candidate(kind="album", title="Dummy", artist="Portishead",
                         detail="1994 · 16-bit/44.1 kHz · 11 tracks",
                         payload={"album_id": "al1"}, selected=False)
    parked.add_candidate(kind="album", title="Third", artist="Portishead",
                         detail="2008 · 24-bit/44.1 kHz · 10 tracks",
                         payload={"album_id": "al2"}, selected=False)
    parked.status = jm.JobStatus.AWAITING_REVIEW
    parked.set_selected(parked.candidates[0]["cid"], True)
    jm.registry.add(parked)

    scan = jm.Job(title="Library scan")
    scan.execute_kind = "library"
    scan.status = jm.JobStatus.SCANNING
    scan.add_candidate(kind="album", title="Dummy", artist="Portishead",
                       detail="1994 · 16-bit/44.1 kHz · 11 tracks",
                       payload={"album_id": "al1"}, selected=False)
    scan.add_candidate(kind="album", title="Roseland NYC Live",
                       artist="Portishead",
                       detail="1998 · 16-bit/44.1 kHz · 11 tracks",
                       payload={"album_id": "al3"}, selected=False)
    jm.registry.add(scan)
    try:
        webapp._fold_into_parked_library_review(scan)

        assert scan.status == jm.JobStatus.DONE
        assert scan.candidates == []
        assert "Folded 1 new find" in (scan.summary or "")
        ids = [c["payload"]["album_id"] for c in parked.candidates]
        assert ids == ["al1", "al2", "al3"]
        ticked = [c["payload"]["album_id"]
                  for c in parked.candidates if c.get("selected")]
        assert ticked == ["al1"]
        assert parked.status == jm.JobStatus.AWAITING_REVIEW
        library_reviews = [j for j in jm.registry.awaiting_review()
                           if j.execute_kind == "library"]
        assert library_reviews == [parked]
    finally:
        _remove_job(parked)
        _remove_job(scan)


def test_fold_swaps_candidate_class_and_keeps_the_tick(monkeypatch):
    """An album that changed on disk while the review sat parked (missing →
    partially added, or a gapped album deleted by hand) must swap to the fresh
    candidate class instead of being silently swallowed as a duplicate key —
    and the user's tick must survive the swap."""
    from qobuz_librarian.web import app as webapp

    parked = jm.Job(title="Library scan")
    parked.execute_kind = "library"
    parked.add_candidate(kind="album", title="The White EP", artist="Agalloch",
                         detail="2019 · fully missing · 8 tracks",
                         payload={"album_id": "wx1"}, selected=True)
    parked.add_candidate(kind="album", title="Ashes", artist="Agalloch",
                         detail="gap-fill: 3 of 10 tracks missing",
                         payload={"album_id": "ax1", "gap_fill": True},
                         selected=False)
    parked.status = jm.JobStatus.AWAITING_REVIEW
    jm.registry.add(parked)

    scan = jm.Job(title="Library scan")
    scan.execute_kind = "library"
    scan.status = jm.JobStatus.SCANNING
    # The White EP appeared on disk with some tracks → now a gap candidate;
    # Ashes was deleted by hand → now fully missing.
    scan.add_candidate(kind="album", title="The White EP", artist="Agalloch",
                       detail="gap-fill: 5 of 8 tracks missing",
                       payload={"album_id": "wx1", "gap_fill": True},
                       selected=False)
    scan.add_candidate(kind="album", title="Ashes", artist="Agalloch",
                       detail="2005 · fully missing · 10 tracks",
                       payload={"album_id": "ax1"}, selected=False)
    jm.registry.add(scan)
    try:
        webapp._fold_into_parked_library_review(scan)

        from qobuz_librarian.web import flows
        by_id = {c["payload"]["album_id"]: c for c in parked.candidates}
        assert flows.is_gap_candidate(by_id["wx1"])
        assert by_id["wx1"]["selected"] is True
        assert not flows.is_gap_candidate(by_id["ax1"])
        assert by_id["ax1"]["selected"] is False
        assert "Updated 2" in (scan.summary or "")
        assert "up to date" not in (scan.summary or "")
        cids = [c["cid"] for c in parked.candidates]
        assert len(set(cids)) == len(cids)
    finally:
        _remove_job(parked)
        _remove_job(scan)


def test_fold_carries_the_scan_honesty_caveat(monkeypatch):
    from qobuz_librarian.web import app as webapp

    parked = jm.Job(title="Library scan")
    parked.execute_kind = "library"
    parked.add_candidate(kind="album", title="A", artist="X",
                         payload={"album_id": "a1"}, selected=False)
    parked.status = jm.JobStatus.AWAITING_REVIEW
    jm.registry.add(parked)

    scan = jm.Job(title="Library scan")
    scan.execute_kind = "library"
    scan.status = jm.JobStatus.SCANNING
    scan._unchecked_artists = 10
    jm.registry.add(scan)
    try:
        webapp._fold_into_parked_library_review(scan)

        assert "10 artists couldn't be checked" in (scan.summary or "")
        assert "up to date" not in (scan.summary or "")
    finally:
        _remove_job(parked)
        _remove_job(scan)


def test_fold_does_not_resurrect_albums_dismissed_during_the_refresh(
        monkeypatch, tmp_path):
    from qobuz_librarian.library import hidden as hidden_mod
    from qobuz_librarian.web import app as webapp

    parked = jm.Job(title="Library scan")
    parked.execute_kind = "library"
    parked.status = jm.JobStatus.AWAITING_REVIEW
    jm.registry.add(parked)

    scan = jm.Job(title="Library scan")
    scan.execute_kind = "library"
    scan.status = jm.JobStatus.SCANNING
    scan.add_candidate(kind="album", title="Dismissed Mid-Scan", artist="X",
                       payload={"album_id": "d1"}, selected=False)
    jm.registry.add(scan)
    # Hidden AFTER the scan built its candidate list — the stale-snapshot case.
    hidden_mod.hide(hidden_mod.SCOPE_MISSING, [("X", "Dismissed Mid-Scan", "")])
    try:
        webapp._fold_into_parked_library_review(scan)

        assert parked.candidates == []
        assert "No new finds" in (scan.summary or "")
    finally:
        hidden_mod.restore(hidden_mod.SCOPE_MISSING, ["X"])
        _remove_job(parked)
        _remove_job(scan)


def test_fold_skips_a_review_approved_mid_refresh(monkeypatch):
    """Approve flips the review out of AWAITING_REVIEW between scan finish
    and fold — the refresh must keep its candidates and park normally instead
    of leaking finds into the executing job."""
    from qobuz_librarian.web import app as webapp

    parked = jm.Job(title="Library scan")
    parked.execute_kind = "library"
    parked.add_candidate(kind="album", title="A", artist="X",
                         payload={"album_id": "a1"})
    parked.status = jm.JobStatus.AWAITING_REVIEW
    jm.registry.add(parked)

    scan = jm.Job(title="Library scan")
    scan.execute_kind = "library"
    scan.status = jm.JobStatus.SCANNING
    scan.add_candidate(kind="album", title="B", artist="Y",
                       payload={"album_id": "b1"}, selected=False)
    jm.registry.add(scan)
    try:
        parked.status = jm.JobStatus.PENDING  # approve won the race
        webapp._fold_into_parked_library_review(scan)

        assert scan.status == jm.JobStatus.SCANNING
        assert len(scan.candidates) == 1
        assert len(parked.candidates) == 1
    finally:
        _remove_job(parked)
        _remove_job(scan)


def test_restore_refolds_into_the_parked_library_review():
    from qobuz_librarian.library import library_scan_state
    from qobuz_librarian.web import flows

    parked = jm.Job(title="Library scan")
    parked.execute_kind = "library"
    parked.status = jm.JobStatus.AWAITING_REVIEW
    jm.registry.add(parked)
    library_scan_state.save_kind("missing", artists={
        "Agalloch": {"fingerprint": "fp", "candidates": [
            {"kind": "album", "title": "Ashes Against the Grain",
             "artist": "Agalloch", "detail": "2006 · fully missing",
             "payload": {"album_id": "ag1"}},
        ]},
    }, complete=True)
    try:
        added = flows.refold_restored_missing(["Agalloch"], [])
        assert added == 1
        assert parked.candidates[0]["payload"]["album_id"] == "ag1"
        assert parked.candidates[0]["selected"] is False
    finally:
        library_scan_state.save_kind("missing", artists={}, complete=False)
        _remove_job(parked)


def test_refresh_without_parked_review_parks_normally(monkeypatch):
    from qobuz_librarian.web import app as webapp

    scan = jm.Job(title="Library scan")
    scan.execute_kind = "library"
    scan.status = jm.JobStatus.SCANNING
    scan.add_candidate(kind="album", title="Dummy", artist="Portishead",
                       payload={"album_id": "al1"}, selected=False)
    jm.registry.add(scan)
    try:
        webapp._fold_into_parked_library_review(scan)

        assert scan.status == jm.JobStatus.SCANNING
        assert len(scan.candidates) == 1
    finally:
        _remove_job(scan)


def test_post_baseline_library_scan_control_recedes(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(
        "qobuz_librarian.library.new_releases.is_baseline_complete",
        lambda: True)
    monkeypatch.setattr(webapp, "_TOKEN_VALID", True)
    monkeypatch.setattr(webapp, "_library_scan_state",
                        lambda: {"ready": True, "count": 3, "message": ""})
    monkeypatch.setattr(webapp, "_last_scan_age", lambda: "2 days ago")
    monkeypatch.setattr(webapp, "_census_view", lambda: None)

    r = client.get("/library")

    assert r.status_code == 200
    assert 'class="ql-header-refresh"' in r.text
    assert "Scan for music added outside the app" in r.text
    assert ">Scan library</button>" not in r.text
    assert ">Check new releases</button>" in r.text


def test_volume_gate_reopens_without_a_restart(tmp_path, monkeypatch):
    # The writability verdict used to be sealed at startup, so fixing the
    # ownership of a root-created music folder left downloads refusing while
    # Diagnostics — which re-checks live — showed green. The gate probes live
    # now and has to agree with Diagnostics.
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import app as webapp

    music = tmp_path / "music"
    music.mkdir()
    monkeypatch.setenv("QL_CHECK_VOLUMES", "1")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "STAGING_DIR", tmp_path / "staging")
    (tmp_path / "staging").mkdir()

    music.chmod(0o500)
    try:
        assert any("MUSIC_ROOT" in item and "read-only" in item
                   for item in webapp._unwritable_volumes())
        assert webapp._web_writes_paused() is True
    finally:
        music.chmod(0o700)

    assert webapp._unwritable_volumes() == []


def test_new_release_check_stays_reachable_while_a_review_is_parked(
        client, monkeypatch):
    # A parked review can live for months, and it hides the baseline-ready
    # strip — the manual check used to live only there, so it was
    # unreachable the whole time.
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(
        "qobuz_librarian.library.new_releases.is_baseline_complete",
        lambda: True)
    monkeypatch.setattr(webapp, "_TOKEN_VALID", True)
    monkeypatch.setattr(webapp, "_library_scan_state",
                        lambda: {"ready": True, "count": 3, "message": ""})
    monkeypatch.setattr(webapp, "_census_view", lambda: None)
    parked = jm.Job(title="Library scan")
    parked.execute_kind = "library"
    parked.status = jm.JobStatus.AWAITING_REVIEW
    jm.registry.add(parked)
    try:
        r = client.get("/library")
        assert r.status_code == 200
        assert ">Check new releases</button>" in r.text
        assert 'class="ql-header-refresh"' in r.text
    finally:
        _remove_job(parked)


def test_pre_baseline_library_keeps_the_scan_hero(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(
        "qobuz_librarian.library.new_releases.is_baseline_complete",
        lambda: False)
    monkeypatch.setattr(webapp, "_TOKEN_VALID", True)
    monkeypatch.setattr(webapp, "_library_scan_state",
                        lambda: {"ready": True, "count": 3, "message": ""})

    r = client.get("/library")

    assert r.status_code == 200
    assert ">Scan library</button>" in r.text
    assert 'class="ql-header-refresh"' not in r.text


def test_quality_shortfall_marks_history_until_the_job_is_opened(
        client, monkeypatch):
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()

    job = jm.Job(title="Dummy", artist="Portishead", album_id="al1")
    job.status = jm.JobStatus.DONE
    job.attention = "quality"
    job.finished_at = time.time()
    job_persistence.persist(job)

    r = client.get("/queue/history")
    assert r.status_code == 200
    assert "Below target quality" in r.text
    assert "data-attention-badge" in r.text

    r = client.get(f"/jobs/{job.id}")
    assert r.status_code == 200

    row = job_persistence.load_one(job.id)
    assert row["attention"] == ""
    r = client.get("/queue/history")
    assert "Below target quality" not in r.text
    assert "data-attention-badge" not in r.text


def test_note_quality_shortfall_flags_the_running_job():
    job = jm.Job(title="Dummy")
    jm._TLS.current_job = job
    try:
        jm.note_quality_shortfall()
    finally:
        jm._TLS.current_job = None
    assert job.attention == "quality"
    assert jm._queue_executor.on_quality_shortfall is jm.note_quality_shortfall


def test_new_release_approve_parks_the_unticked_remnant(client, monkeypatch):
    """A new release stays in the New Releases review until it's downloaded or
    dismissed: approving 1 of 2 must park the other as its own new-release
    review, not consume it (the persistent baseline already recorded it, so
    nothing else would ever offer it again)."""
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_qobuz_ready", lambda: True)
    job = jm.Job(title="New-release check")
    job.execute_kind = "new_releases"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job.add_candidate("album", "Wanted", "X", payload={"album_id": "a1"},
                      selected=True)
    job.add_candidate("album", "Later", "X", payload={"album_id": "a2"},
                      selected=False)
    job._execute_fn = lambda j, chosen: None
    jm.registry.add(job)
    remnant = None
    try:
        r = client.post(f"/jobs/{job.id}/approve", data={"tab": ""},
                        follow_redirects=False)
        assert r.status_code == 303
        assert {c["title"] for c in job.candidates} == {"Wanted"}
        remnant = next(
            (j for j in jm.registry.awaiting_review()
             if j.id != job.id and j.execute_kind == "new_releases"), None)
        assert remnant is not None
        assert {c["title"] for c in remnant.candidates} == {"Later"}
    finally:
        _remove_job(job)
        if remnant is not None:
            _remove_job(remnant)


def test_failed_new_release_pick_returns_to_a_new_release_review():
    """A failed new-release download wasn't downloaded — it folds back into
    the parked New Releases review (or re-parks a fresh one), ticked, instead
    of being consumed with the dead job. It must never land in a Library
    review."""
    from qobuz_librarian.web import flows

    parked = jm.Job(title="New-release check")
    parked.execute_kind = "new_releases"
    parked.status = jm.JobStatus.AWAITING_REVIEW
    parked.add_candidate("album", "Untouched", "X",
                         payload={"album_id": "a1"}, selected=False)
    jm.registry.add(parked)
    library = jm.Job(title="Library scan")
    library.kind = "scan"
    library.execute_kind = "library"
    library.status = jm.JobStatus.AWAITING_REVIEW
    library.add_candidate("album", "LibThing", "Y", payload={"album_id": "L1"})
    jm.registry.add(library)
    try:
        flows._return_new_release_picks(
            [{"kind": "album", "title": "Failed NR", "artist": "X",
              "detail": "", "payload": {"album_id": "a2"}}])
        titles = {c["title"] for c in parked.candidates}
        assert titles == {"Untouched", "Failed NR"}
        failed = next(c for c in parked.candidates if c["title"] == "Failed NR")
        assert failed["selected"] is True
        assert {c["title"] for c in library.candidates} == {"LibThing"}
    finally:
        _remove_job(parked)
        _remove_job(library)


def test_partial_import_folds_an_instant_gap_fill_candidate():
    """An album that lands with some tracks failed becomes a Gap Fill
    candidate in the living Library review immediately — unticked, honest
    detail — instead of waiting for the next manual refresh."""
    from qobuz_librarian.web import flows

    parked = jm.Job(title="Library scan")
    parked.kind = "scan"
    parked.execute_kind = "library"
    parked.status = jm.JobStatus.AWAITING_REVIEW
    parked.add_candidate("album", "Existing", "Y", payload={"album_id": "L1"})
    jm.registry.add(parked)
    try:
        flows._fold_partial_gap_fill(
            {"id": "a9", "title": "Short Album", "tracks_count": 10},
            "Artist", 3)
        gap = next(c for c in parked.candidates
                   if c["title"] == "Short Album")
        assert gap["selected"] is False
        assert (gap.get("payload") or {}).get("gap_fill") == 3
        assert "gap-fill: 3 missing of 10" in (gap.get("detail") or "")
    finally:
        _remove_job(parked)


def test_partial_new_release_download_returns_to_the_nr_review(monkeypatch):
    """A New Releases download that lands only partly isn't downloaded — the
    release goes back to the New Releases review (ticked, like a failure), and
    its remainder must NOT leak into the Library review as Gap Fill."""
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows

    parked_nr = _inject_job(jm.JobStatus.AWAITING_REVIEW, "New-release check")
    parked_nr.execute_kind = "new_releases"
    parked_lib = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
    parked_lib.execute_kind = "library"
    running = _inject_job(jm.JobStatus.RUNNING, "New-release check")
    running.execute_kind = "new_releases"
    running.add_candidate(kind="album", title="Fresh Drop", artist="Abigail",
                          payload={"album_id": "nr1"}, selected=True)
    chosen = list(running.candidates)
    monkeypatch.setattr(flows.cfg, "ARTIST_API_DELAY", 0)
    monkeypatch.setattr(flows, "get_album",
                        lambda aid, _t: {"id": aid, "title": "Fresh Drop",
                                         "tracks_count": 10})
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "_refresh_after_local_album_change",
                        lambda *a, **k: None)
    monkeypatch.setattr(process_mod, "process_album",
                        lambda *_a, **_k: {"imported": True, "n_ok": 7,
                                           "n_fail": 3, "result": "downloaded"})
    try:
        flows.execute_albums(running, chosen, "tok")
        titles = {c["title"] for c in parked_nr.candidates}
        assert "Fresh Drop" in titles
        back = next(c for c in parked_nr.candidates if c["title"] == "Fresh Drop")
        assert back["selected"] is True
        assert parked_lib.candidates == []
    finally:
        _remove_job(parked_nr)
        _remove_job(parked_lib)
        _remove_job(running)


def test_dismiss_honours_a_tick_saved_during_the_store_write(monkeypatch):
    """dismiss_albums writes the durable store outside the job lock; a tick
    landing in that window was promised "keep the ticked ones". The row must
    survive AND its just-written dismissal must be taken back out of the
    store."""
    from qobuz_librarian.library import hidden as hidden_mod
    from qobuz_librarian.web import flows

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
    job.execute_kind = "library"
    job.add_candidate(kind="album", title="Kept", artist="Abigail",
                      payload={"album_id": "k1", "year": 2020}, selected=False)
    cand = job.candidates[0]
    restored = []

    def hide_and_race(scope, specs):
        # The user's tick lands while the store write is in flight.
        cand["selected"] = True
        return len(list(specs))

    monkeypatch.setattr(hidden_mod, "hide", hide_and_race)
    monkeypatch.setattr(hidden_mod, "restore_albums",
                        lambda scope, fps: restored.extend(fps))
    try:
        n = flows.dismiss_albums(job, "Abigail")
        assert n == 0
        assert [c["title"] for c in job.candidates] == ["Kept"]
        assert job.candidates[0]["selected"] is True
        assert restored == [hidden_mod.album_fingerprint("Abigail", "Kept")]

        # A route can pass its opening status check and then lose a race to
        # approval before the off-thread store write begins.
        cand["selected"] = False
        job.status = jm.JobStatus.PENDING
        assert flows.dismiss_albums(job, "Abigail") is None
        assert cand["selected"] is False
    finally:
        _remove_job(job)


def test_new_edition_download_folds_onto_an_identical_running_job():
    """"Get this edition too" deliberately skips the owned-album fold, but two
    identical new-edition submits are the same tap twice — the second folds
    onto the in-flight job instead of queueing a concurrent duplicate."""
    from qobuz_librarian.web import app as web_app

    running = _inject_job(jm.JobStatus.RUNNING, "Album — edition")
    running.album_id = "ALB9"
    running.execute_args = {"new_edition": True}
    other = _inject_job(jm.JobStatus.RUNNING, "Other — edition")
    other.album_id = "OTHER"
    other.execute_args = {"new_edition": True}
    try:
        assert web_app._duplicate_download_job("ALB9", "", True) is running
        assert web_app._duplicate_download_job("UNSEEN", "", True) is None
    finally:
        _remove_job(running)
        _remove_job(other)


def test_approve_rechecks_the_write_pause_after_awaits(client, monkeypatch):
    """set_mode('cli') can land between approve's opening gate and the enqueue
    (form parsing and disk probes await in between); the not-yet-approved
    review is invisible to the handoff's active-job check, so only a recheck
    right before consuming the review can see the pause."""
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    job = job_mgr.Job(title="Library migration")
    job.execute_kind = "migration"
    job.status = job_mgr.JobStatus.AWAITING_REVIEW
    job._execute_fn = lambda j, chosen: None
    job.add_candidate("album", "A", "Artist", payload={"id": 1})
    job.candidates[0]["selected"] = True
    job_mgr.registry.add(job)

    # Simulate losing the race: the opening gate already passed, then the
    # CLI handoff flipped the mode before the enqueue.
    monkeypatch.setattr(webapp, "_lock_busy_response", lambda req: None)
    monkeypatch.setattr(webapp, "_CLI_MODE", True)

    r = client.post(f"/jobs/{job.id}/approve", follow_redirects=False)

    assert r.status_code in (200, 303, 503)
    assert job.status == job_mgr.JobStatus.AWAITING_REVIEW
    assert any(c.get("selected") for c in job.candidates)


def test_settings_storage_separates_the_library_from_the_drive(client, monkeypatch):
    # The one line used to read "Music folder: 3.31 TB used", which is the whole
    # filesystem — on an instance holding 1.6 MB of music.
    import shutil

    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_read_creds",
                        lambda: {"user_id": "u", "auth_token": "t"})
    monkeypatch.setattr(app_mod, "_census_view",
                        lambda: {"total": "38,201 tracks · 412 GB"})
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda _p: type("du", (), {"used": 3_310_000_000_000,
                                   "free": 520_000_000_000,
                                   "total": 3_830_000_000_000})())

    monkeypatch.setattr(app_mod, "_is_mount_point", lambda _p: True)
    r = client.get("/settings")
    assert "Your library: 38,201 tracks · 412 GB" in r.text
    assert "Music drive: 520 GB free of 3.83 TB" in r.text
    assert "Music folder: 3.31 TB used" not in r.text

    # Sharing a disk with everything else says so instead of implying the
    # figures are the music's own.
    monkeypatch.setattr(app_mod, "_is_mount_point", lambda _p: False)
    r = client.get("/settings")
    assert "Drive holding your music: 520 GB free of 3.83 TB" in r.text


def test_stale_csrf_gets_a_readable_page_and_a_usable_token(client):
    # The old reply was text/plain "CSRF token missing or invalid" with no nav,
    # and because it wasn't HTML the middleware skipped minting a cookie too —
    # so the retry failed identically.
    client.cookies.clear()
    r = client.post("/settings/behavior", data={"form_complete": "1"},
                    follow_redirects=False)

    assert r.status_code == 403
    assert "CSRF token missing or invalid" not in r.text
    assert "went stale" in r.text.lower()
    assert "Reload and try again" in r.text
    assert "ql_csrf" in r.headers.get("set-cookie", "")

    # And an htmx action gets told to reload rather than swallowing a 403.
    client.cookies.clear()
    r = client.post("/settings/behavior", data={"form_complete": "1"},
                    headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.headers.get("HX-Refresh") == "true"
    assert "ql_csrf" in r.headers.get("set-cookie", "")


def test_connection_badge_reports_only_what_qobuz_has_confirmed(client, monkeypatch):
    # "Connected" used to show for any saved token, including one that had never
    # authenticated — and a token Qobuz had rejected.
    import qobuz_librarian.web.app as app_mod
    monkeypatch.setattr(app_mod, "_read_creds",
                        lambda: {"user_id": "u", "auth_token": "t"})

    monkeypatch.setattr(app_mod, "_TOKEN_VALID", True)
    assert "Connected" in client.get("/settings").text

    monkeypatch.setattr(app_mod, "_TOKEN_VALID", None)
    body = client.get("/settings").text
    assert "Token saved" in body and ">Connected" not in body

    monkeypatch.setattr(app_mod, "_TOKEN_VALID", False)
    body = client.get("/settings").text
    assert "Token not authenticating" in body
    assert "Connected" not in body


def test_blank_login_does_not_spend_a_strike(client, monkeypatch):
    # Five blank taps used to lock the owner out of his own app for an hour.
    from qobuz_librarian.web import auth as web_auth

    monkeypatch.setenv("WEB_AUTH", "on")
    # The auth middleware reads the credential file itself, so a configured box
    # has to be faked there as well as at the route.
    monkeypatch.setattr(web_auth, "_read", lambda: {
        "username": "dink", "password_hash": "x", "session_secret": "s"})
    monkeypatch.setattr(web_auth, "credentials_configured", lambda: True)
    calls = []
    monkeypatch.setattr(web_auth, "record_login_failure",
                        lambda *a, **k: calls.append(a))
    monkeypatch.setattr(web_auth, "verify_login",
                        lambda *a, **k: calls.append("verified") or False)

    r = client.post("/login", data={"username": "", "password": ""},
                    follow_redirects=False)

    assert r.status_code == 400
    assert "Enter your username and password." in r.text
    assert calls == [], "a blank submit must not reach the throttle or the KDF"


def test_lockout_says_how_long_is_left_and_keeps_the_username(client, monkeypatch):
    from qobuz_librarian.web import auth as web_auth

    monkeypatch.setenv("WEB_AUTH", "on")
    # The auth middleware reads the credential file itself, so a configured box
    # has to be faked there as well as at the route.
    monkeypatch.setattr(web_auth, "_read", lambda: {
        "username": "dink", "password_hash": "x", "session_secret": "s"})
    monkeypatch.setattr(web_auth, "credentials_configured", lambda: True)
    monkeypatch.setattr(web_auth, "check_login_rate_limit", lambda *a, **k: False)
    monkeypatch.setattr(web_auth, "login_lockout_remaining", lambda *a, **k: 903)

    r = client.post("/login", data={"username": "dink", "password": "x"},
                    follow_redirects=False)

    assert r.status_code == 429
    assert "Try again in 16 minutes" in r.text
    assert "restart Qobuz Librarian to clear it" in r.text
    assert "Wait an hour" not in r.text
    assert 'value="dink"' in r.text
