"""Tests for the web UI: background job system (jobs.py) and HTTP routes (app.py).

Trimmed to a maintainable representative set: data-safety paths (restore,
hide/restore round-trip, migration move-vs-copy, persist-without-tearing),
auth/session/CSRF, the run-lock destructive-route guard, settings save/load,
one search + one approve endpoint, and a few genuinely tricky bits of logic.
"""

import asyncio
import concurrent.futures
import copy
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from qobuz_librarian.web import jobs as jm

# ── jobs.py: Job ──────────────────────────────────────────────────────────────




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

    def allow(candidate):
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


def test_worker_readmission_failure_finishes_and_notifies(monkeypatch):
    registry = jm.JobRegistry()
    job = jm.Job(id="worker-admission-failed", title="Album")
    job._durability_required = True
    registry.add(job)
    stop = threading.Event()
    ran = []
    persisted = []
    sent = []

    class OneItemQueue:
        def get(self, timeout):
            stop.set()
            return job, lambda _job: ran.append(True)

        @staticmethod
        def task_done():
            return None

    monkeypatch.setattr(jm, "registry", registry)
    monkeypatch.setattr(jm, "_stop_event", stop)
    monkeypatch.setattr(jm.job_persistence, "admit", lambda _job: False)
    monkeypatch.setattr(
        jm.job_persistence,
        "persist",
        lambda current: persisted.append(current.status.value) or True,
    )
    monkeypatch.setattr(
        jm,
        "_start_post_job_hook",
        lambda payload: sent.append(
            (list(persisted), payload["id"], payload["status"])
        ),
    )

    jm._worker_loop(OneItemQueue())

    assert ran == []
    assert job.status is jm.JobStatus.FAILED
    assert persisted == ["failed"]
    assert sent == [(["failed"], job.id, "failed")]


@pytest.mark.parametrize(
    ("command", "timeout", "expected"),
    [
        ("exit 17", 1, "post-job hook exited with status 17"),
        ("sleep 5", 0.01, "post-job hook timed out"),
    ],
)
def test_post_job_hook_reports_command_failures(
    monkeypatch, caplog, command, timeout, expected,
):
    from qobuz_librarian import config

    monkeypatch.setenv("POST_JOB_HOOK", command)
    monkeypatch.setattr(config, "POST_JOB_HOOK_TIMEOUT", timeout)

    with caplog.at_level("WARNING", logger="qobuz_librarian"):
        jm._run_post_job_hook({"id": "inert-hook-check"})

    assert expected in caplog.text


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


def test_post_job_hook_receives_the_terminal_snapshot(monkeypatch):
    registry = jm.JobRegistry()
    job = jm.Job(id="hook-snapshot", title="Album", artist="Artist")
    job.status = jm.JobStatus.FAILED
    job.error = "download failed"
    job.finished_at = 123.0
    registry.add(job)
    received = []

    monkeypatch.setattr(jm, "registry", registry)
    monkeypatch.setattr(jm.job_persistence, "persist", lambda _job: True)
    monkeypatch.setattr(
        jm,
        "_start_post_job_hook",
        lambda payload: received.append(dict(payload)),
    )

    jm._finish_task_phase(job)
    job.status = jm.JobStatus.PENDING
    job.error = None
    job.finished_at = None

    assert received[0]["status"] == "failed"
    assert received[0]["error"] == "download failed"
    assert received[0]["finished_at"] == 123.0


def test_late_cancel_cannot_relabel_proven_durable_download(monkeypatch):
    from types import SimpleNamespace

    from qobuz_librarian.library import catalog, hidden
    from qobuz_librarian.queue import durable_album, executor
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import flows

    album = {
        "id": "late-cancel-album",
        "title": "Completed Album",
        "artist": {"name": "Artist"},
        "tracks": {"items": [{"id": "track-1"}]},
    }
    durable_item = {"album": album}
    monkeypatch.setattr(catalog, "is_lossless_album", lambda _album: True)
    monkeypatch.setattr(catalog, "find_existing_tracks", lambda _album: ([], None))
    monkeypatch.setattr(
        catalog,
        "compute_missing",
        lambda tracks, _existing: (list(tracks), []),
    )
    monkeypatch.setattr(
        "qobuz_librarian.queue.builder._build_queue_item",
        lambda **_kwargs: durable_item,
    )
    monkeypatch.setattr(durable_album, "plan_durable_new_album", lambda *_args: object())
    monkeypatch.setattr(flows, "build_args", lambda: SimpleNamespace())
    monkeypatch.setattr(flows, "_note_staging_wait", lambda *_args: None)
    monkeypatch.setattr(flows, "_refresh_after_local_album_change", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(flows, "prune_library_review_candidates", lambda *_args: None)
    monkeypatch.setattr(hidden, "unmark_single", lambda *_args, **_kwargs: None)

    job = jm.Job(
        title=album["title"],
        artist=album["artist"]["name"],
        album_id=album["id"],
    )
    job.status = jm.JobStatus.RUNNING

    def exact_completion(_queue, _args, _token, **_kwargs):
        job.cancel_requested = True
        return (
            [
                {
                    "result": "downloaded",
                    "imported": True,
                    "n_ok": 1,
                    "n_fail": 0,
                    "n_lossy": 0,
                }
            ],
            True,
        )

    monkeypatch.setattr(executor, "_execute_download_queue", exact_completion)
    monkeypatch.setattr(webapp, "_run_lock_intact", lambda: True)
    monkeypatch.setattr(
        webapp,
        "_record_startup_recovery",
        lambda _authority: SimpleNamespace(status=SimpleNamespace(value="clear")),
    )
    monkeypatch.setattr(webapp, "_RUN_LOCK_HANDLE", object())
    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    monkeypatch.setattr(jm.job_persistence, "persist", lambda _job: True)
    monkeypatch.setattr(jm, "_fire_post_job_hook", lambda _job: None)
    jm.registry.add(job)

    jm._run_task(job, webapp._make_download_run(album, "token"))

    assert job.status is jm.JobStatus.DONE
    assert job.cancel_requested is False


def test_delayed_selection_save_reports_a_write_failure(monkeypatch):
    registry = jm.JobRegistry()
    job = jm.Job(id="unsaved-selection", title="Review")
    job.status = jm.JobStatus.AWAITING_REVIEW
    registry.add(job)
    subscriber = job.subscribe()
    monkeypatch.setattr(jm, "registry", registry)
    monkeypatch.setattr(jm, "_PERSIST_DELAY", 0.01)
    monkeypatch.setattr(jm.job_persistence, "persist", lambda _job: False)

    jm.persist_soon(job)

    assert subscriber.get(timeout=1) == jm.REVIEW_CHANGED + "save_failed"
    job.unsubscribe(subscriber)


def test_one_line_job_log_cap_keeps_the_latest_line():
    job = jm.Job(title="bounded log")
    job.LOG_CAP = 1
    for number in range(job._LOG_SLACK + 2):
        job.push_line(f"line {number}")

    assert job.log_lines == [f"line {job._LOG_SLACK + 1}"]


def test_per_artist_rescan_supersedes_only_that_artists_parked_review(
        monkeypatch):
    # Two artists each have a scan parked for review.
    from qobuz_librarian.web import app as app_mod
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(job_persistence, "persist", lambda _job: True)

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
    # grab is its own thing, and neither should be swallowed by an unrelated job for
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
    # queued for anything, so refusing an explicit /download with "already
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


def test_web_download_surfaces_a_retained_backup_without_calling_it_lossy(
        client, monkeypatch):
    import contextlib
    from types import SimpleNamespace
    from urllib.parse import unquote

    from qobuz_librarian.library import catalog as catalog_mod
    from qobuz_librarian.library import hidden as hidden_mod
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import app as app_mod
    from qobuz_librarian.web import flows

    album = {
        "id": "unverified-recovery",
        "title": "Album",
        "artist": {"name": "Artist"},
    }
    outcome = {
        "result": "cancelled",
        "imported": False,
        "n_ok": 1,
        "n_fail": 0,
        "n_lossy": 0,
        "n_broken": 0,
        "n_lossy_only": 0,
        "recovery_unverified": True,
    }
    monkeypatch.setattr(catalog_mod, "is_lossless_album", lambda _album: False)
    monkeypatch.setattr(
        process_mod,
        "process_album",
        lambda *_a, **_k: dict(outcome),
    )
    monkeypatch.setattr(flows, "build_args", lambda: SimpleNamespace())
    monkeypatch.setattr(flows, "_note_staging_wait", lambda *_a, **_k: None)
    monkeypatch.setattr(
        flows, "_refresh_after_local_album_change", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        flows, "prune_library_review_candidates", lambda *_a, **_k: None
    )
    monkeypatch.setattr(hidden_mod, "unmark_single", lambda *_a, **_k: None)
    monkeypatch.setattr(jm, "staging_lock", contextlib.nullcontext)

    job = jm.Job(
        title=album["title"],
        artist=album["artist"]["name"],
        album_id=album["id"],
        status=jm.JobStatus.RUNNING,
    )
    app_mod._make_download_run(album, "tok")(job)

    assert job.status is jm.JobStatus.FAILED
    assert job.attention == "backup"
    assert job.execute_args["retry_disabled"] == "backup"
    assert "backup" in job.error.lower()
    assert "lossy" not in job.error.lower()
    assert "backup" in job.summary.lower()
    assert "discarded" not in job.summary.lower()
    jm.registry.add(job)
    try:
        response = client.post(f"/jobs/{job.id}/retry", follow_redirects=False)
        assert response.status_code == 303
        assert "retained safety backup" in unquote(response.headers["location"])
    finally:
        _remove_job(job)


def test_web_album_batch_marks_an_unverified_upgrade_as_attention(monkeypatch):
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows

    _allow_legacy_candidate_execution(monkeypatch)

    album = {
        "id": "unverified-upgrade",
        "title": "Album",
        "artist": {"name": "Artist"},
    }
    monkeypatch.setattr(flows.cfg, "ARTIST_API_DELAY", 0)
    monkeypatch.setattr(flows, "get_album", lambda _aid, _token: album)
    folded = []
    monkeypatch.setattr(
        process_mod,
        "process_album",
        lambda *_a, **_k: {
            "result": "partial",
            "imported": True,
            "n_ok": 10,
            "n_fail": 0,
            "n_lossy": 0,
            "n_broken": 0,
            "n_lossy_only": 0,
            "upgrade_unverified": True,
        },
    )
    monkeypatch.setattr(
        flows, "_refresh_after_local_album_change", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        flows, "prune_library_review_candidates", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        flows,
        "_fold_partial_gap_fill",
        lambda *_a, **_k: folded.append((_a, _k)),
    )

    job = jm.Job(title="Library downloads", status=jm.JobStatus.RUNNING)
    job.execute_kind = "library"
    flows.execute_albums(
        job,
        [{
            "artist": "Artist",
            "title": "Album",
            "payload": {"album_id": album["id"]},
        }],
        "tok",
    )

    assert job.status is jm.JobStatus.FAILED
    assert job.attention == "backup"
    assert "backup" in job.summary.lower()
    assert "lossy" not in job.error.lower()
    assert folded == []




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


def test_pending_cancel_rolls_back_when_terminal_state_cannot_be_saved(monkeypatch):
    job = jm.Job(title="unsaved queued cancel")
    job._durability_required = True
    monkeypatch.setattr(jm.job_persistence, "_persist_locked", lambda _job: False)

    assert jm.request_cancel(job) is False
    assert job.status == jm.JobStatus.PENDING
    assert job.cancel_requested is False
    assert job.finished_at is None




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
    from qobuz_librarian.api.auth import credentials_from_values
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
    qobuz_credentials = credentials_from_values(
        "test-user",
        "test-token",
        source="streamrip",
    )

    async def _authorize_for_web(*_args, **_kwargs):
        return qobuz_credentials

    monkeypatch.setattr(app_mod, "_authorize_qobuz_for_web", _authorize_for_web)
    monkeypatch.setattr(
        app_mod,
        "_authorize_qobuz_live",
        lambda *_args, **_kwargs: qobuz_credentials,
    )
    monkeypatch.setattr(
        app_mod,
        "_credential_generation_is_active",
        lambda generation: generation == qobuz_credentials.generation,
    )

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


def test_health_separates_liveness_from_readiness(client, monkeypatch,
                                                   tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import app as app_mod
    from qobuz_librarian.web import auth as web_auth
    from qobuz_librarian.web import job_persistence

    run_lock_handle = app_mod._RUN_LOCK_HANDLE
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", data_dir)
    monkeypatch.setenv("WEB_AUTH", "on")
    monkeypatch.setattr(
        web_auth, "creds_file_present_but_unreadable", lambda: False)
    monkeypatch.setattr(app_mod, "_unwritable_volumes", lambda: [])
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

    data_dir.mkdir()
    monkeypatch.setattr(app_mod, "_LOCK_UNENFORCEABLE", True)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"] == ["run_lock"]
    assert client.request("HEAD", "/readyz").status_code == 503

    monkeypatch.setattr(app_mod, "_LOCK_UNENFORCEABLE", False)
    monkeypatch.setattr(
        app_mod, "_unwritable_volumes", lambda: ["MUSIC_ROOT=/music"])
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "status": "degraded",
        "checks": ["write_volumes"],
    }
    assert "/music" not in response.text

    monkeypatch.setattr(app_mod, "_unwritable_volumes", lambda: [])
    assert client.get("/readyz").json() == {"ok": True, "status": "ready"}

    monkeypatch.setattr(app_mod, "_RUN_LOCK_HANDLE", None)
    monkeypatch.setattr(app_mod, "_LOCK_BUSY_PID", 4321)
    monkeypatch.setattr(job_persistence, "_schema_ready", False)
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "status": "degraded",
        "checks": ["other_writer"],
    }

    monkeypatch.setattr(app_mod, "_RUN_LOCK_HANDLE", run_lock_handle)
    monkeypatch.setattr(app_mod, "_LOCK_BUSY_PID", None)
    (data_dir / "jobs.db").mkdir()
    job_persistence._schema_ready = False
    job_persistence.init()
    assert job_persistence.ready_for_admission() is False
    assert job_persistence.persist(jm.Job(title="refused")) is False
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"] == ["job_persistence"]
    assert client.get("/healthz").status_code == 200






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


def test_large_artist_catalog_keeps_results_without_caching_both_views(
        client, monkeypatch):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as catalog_mod
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(catalog_mod, "find_album_dir_filesystem", lambda _a: None)
    count = app_mod._SEARCH_SNAPSHOT_RESULT_LIMIT + 1
    releases = [{
        "id": f"catalog-{index}",
        "title": f"Release {index:03d}",
        "artist": {"name": "Large Catalog"},
        "year": 2000 + index % 20,
        "tracks_count": 10,
        "maximum_bit_depth": 16,
    } for index in range(count)]
    monkeypatch.setattr(
        search_mod,
        "get_artist_albums",
        lambda *_args, **_kwargs: (releases, count),
    )

    response = client.post(
        "/search",
        data={
            "q": "Large Catalog",
            "kind": "artist",
            "artist_id": "large-catalog",
            "artist_name": "Large Catalog",
        },
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert f"{count} albums on Qobuz" in response.text
    assert 'data-search-cacheable="0"' in response.text
    assert response.text.count("<template data-search-view-template>") == 2
    for index in (0, count - 1):
        field = f'name="album_id" value="catalog-{index}"'
        assert response.text.count(field) == 2




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
    tracks = [{"id": f"track{n}"} for n in range(1, 11)]
    exact_album = {**album, "tracks": {"items": tracks}}
    owned = tmp_path / "Das Tor (2013)"
    owned.mkdir()
    for n in range(1, 11):
        (owned / f"{n:02d} - Das Tor.flac").write_bytes(b"\x00")

    monkeypatch.setattr(search_mod, "search_albums", lambda *_a, **_kw: [album])
    monkeypatch.setattr(search_mod, "get_album", lambda *_a, **_kw: exact_album)
    monkeypatch.setattr(catalog_mod, "find_album_dir_filesystem", lambda _a: owned)
    monkeypatch.setattr(catalog_mod, "find_existing_tracks",
                        lambda *_a, **_kw: (tracks, owned))
    monkeypatch.setattr(catalog_mod, "compute_missing",
                        lambda _want, have: ([], have))

    r = client.post("/search", data={"q": "Das Tor", "kind": "album"},
                    headers={"HX-Request": "true"})

    assert r.status_code == 200
    assert "In library" in r.text
    assert "quality-upgrade" not in r.text
    assert ">Upgrade<" not in r.text


def test_new_edition_download_rechecks_exact_ownership(
        client, monkeypatch, tmp_path):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as catalog_mod
    import qobuz_librarian.web.app as app_mod

    album = {
        "id": "remaster",
        "title": "Variance",
        "artist": {"name": "The Lab"},
        "release_date_original": "2024-01-01",
        "tracks": {"items": [
            {"id": "remaster-1", "title": "Track 1"},
            {"id": "remaster-2", "title": "Track 2"},
        ]},
    }
    folder = [None]

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
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
        app_mod,
        "_make_download_run",
        lambda *_args, **_kwargs: (lambda _job: None),
    )
    monkeypatch.setattr(
        app_mod.job_mgr,
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


def test_album_search_marks_a_part_finished_album_as_partial(client, monkeypatch, tmp_path):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as catalog_mod
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    album = {
        "id": "album1",
        "title": "Das Tor",
        "artist": {"name": "Paysage d'Hiver"},
        "year": 2013,
        "tracks_count": 2,
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 96,
    }
    tracks = [{"id": "track1"}, {"id": "track2"}]
    exact_album = {**album, "tracks": {"items": tracks}}
    partial = tmp_path / "Das Tor (2013)"
    partial.mkdir()
    (partial / "01 - Das Tor.flac").write_bytes(b"\x00")
    (partial / "Bonus.flac").write_bytes(b"\x00")

    monkeypatch.setattr(search_mod, "search_albums", lambda *_a, **_kw: [album])
    monkeypatch.setattr(search_mod, "get_album", lambda *_a, **_kw: exact_album)
    monkeypatch.setattr(catalog_mod, "find_album_dir_filesystem", lambda _a: partial)
    monkeypatch.setattr(catalog_mod, "find_existing_tracks",
                        lambda *_a, **_kw: ([tracks[0], {"id": "bonus"}], partial))
    monkeypatch.setattr(catalog_mod, "compute_missing",
                        lambda _want, _have: ([tracks[1]], [tracks[0]]))

    r = client.post("/search", data={"q": "Das Tor", "kind": "album"},
                    headers={"HX-Request": "true"})

    assert r.status_code == 200
    assert "In library" not in r.text
    assert "1 of 2" in r.text
    assert r.text.count(">24/96</span>") == 2
    assert 'name="album_id" value="album1"' in r.text   # still downloadable


# The Doors put out a 25-track "50th Anniversary Deluxe Edition" and two plain
# 11-track pressings of Waiting for the Sun, none of which carries a version
# field. That is the shape that breaks a grouped row.
_WAITING = [
    {
        "id": "deluxe",
        "title": "Waiting for the Sun (50th Anniversary Deluxe Edition)",
        "artist": {"name": "The Doors"},
        "year": 1968,
        "tracks_count": 25,
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 192,
    },
    {
        "id": "cd",
        "title": "Waiting for the Sun",
        "artist": {"name": "The Doors"},
        "year": 1968,
        "tracks_count": 11,
        "maximum_bit_depth": 16,
        "maximum_sampling_rate": 44.1,
    },
    {
        "id": "hires",
        "title": "Waiting For The Sun",
        "artist": {"name": "The Doors"},
        "year": 1968,
        "tracks_count": 11,
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 96,
    },
]


def test_album_search_keeps_quality_for_grouped_partial_editions(
        client, monkeypatch, tmp_path):
    import copy

    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as catalog_mod
    import qobuz_librarian.web.app as app_mod

    releases = copy.deepcopy(_WAITING)
    qualities = {
        "deluxe": (24, 192),
        "cd": (16, 44.1),
        "hires": (24, 96),
    }
    exact = {}
    partial_dirs = {}
    for release in releases:
        bits, rate = qualities[release["id"]]
        release["maximum_bit_depth"] = bits
        release["maximum_sampling_rate"] = rate
        tracks = [
            {"id": f'{release["id"]}-1'},
            {"id": f'{release["id"]}-2'},
        ]
        exact[release["id"]] = {**release, "tracks": {"items": tracks}}
        if release["id"] in {"cd", "hires"}:
            directory = tmp_path / release["id"]
            directory.mkdir()
            partial_dirs[release["id"]] = directory

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(
        search_mod, "search_albums", lambda *_a, **_kw: releases
    )
    monkeypatch.setattr(
        search_mod, "get_album", lambda album_id, *_a, **_kw: exact[album_id]
    )
    monkeypatch.setattr(
        catalog_mod,
        "find_album_dir_filesystem",
        lambda album: partial_dirs.get(album["id"]),
    )
    monkeypatch.setattr(
        catalog_mod,
        "find_existing_tracks",
        lambda album, **_kw: ([album["tracks"]["items"][0]],
                              partial_dirs[album["id"]]),
    )
    monkeypatch.setattr(
        catalog_mod,
        "compute_missing",
        lambda wanted, _have: (wanted[1:], wanted[:1]),
    )

    response = client.post(
        "/search",
        data={"q": "Waiting for the Sun", "kind": "album"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "data-version-toggle" in response.text
    assert response.text.count("1 of 2") == 4
    assert response.text.count(">16/44.1</span>") == 2
    assert response.text.count("24/96") == 2
    for album_id in ("cd", "hires"):
        assert response.text.count(
            f'name="album_id" value="{album_id}"'
        ) == 2






def test_search_keeps_release_identities_distinct(client, monkeypatch):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as catalog_mod
    import qobuz_librarian.web.app as app_mod

    releases = [
        {"id": "first-love", "title": "初恋",
         "version": "First Pressing",
         "artist": {"name": "宇多田ヒカル"}, "tracks_count": 12},
        {"id": "innocence", "title": "無罪モラトリアム",
         "artist": {"name": "椎名林檎"}, "tracks_count": 11},
        {"id": "fearless", "title": "Fearless",
         "artist": {"name": "Taylor Swift"}, "tracks_count": 13},
        {"id": "fearless-rerecorded",
         "title": "Fearless (Taylor's Version)",
         "artist": {"name": "Taylor Swift"}, "tracks_count": 26},
    ]
    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(
        search_mod, "search_albums", lambda *_args, **_kwargs: releases
    )
    monkeypatch.setattr(
        catalog_mod, "find_album_dir_filesystem", lambda _album: None
    )

    response = client.post(
        "/search",
        data={"q": "albums", "kind": "album"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "4 albums" in response.text
    assert "data-version-toggle" not in response.text
    table_at = response.text.index('data-search-view-panel="table"')
    grid_at = response.text.index('data-search-view-panel="grid"')
    table = response.text[table_at:grid_at]
    grid = response.text[grid_at:]
    for release in releases:
        field = f'name="album_id" value="{release["id"]}"'
        assert field in table
        assert field in grid
    assert 'Download "無罪モラトリアム" by 椎名林檎?' in table
    assert 'Download "初恋 (First Pressing)" by 宇多田ヒカル?' in table
    assert 'aria-label="Download 初恋 (First Pressing) by 宇多田ヒカル"' in table

    album = {
        "id": "signals",
        "title": "Signals",
        "artist": {"name": "The Lab"},
        "tracks_count": 2,
    }
    monkeypatch.setattr(
        search_mod,
        "search_tracks",
        lambda *_args, **_kwargs: [
            {"id": "studio", "title": "Signal", "version": "Studio Version",
             "album": album},
            {"id": "live", "title": "Signal", "version": "Live Version",
             "album": album},
        ],
    )
    response = client.post(
        "/search",
        data={"q": "Signal", "kind": "track"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    table_at = response.text.index('data-search-view-panel="table"')
    grid_at = response.text.index('data-search-view-panel="grid"')
    table = response.text[table_at:grid_at]
    grid = response.text[grid_at:]
    for version in ("Studio Version", "Live Version"):
        display = f"Signal ({version})"
        assert f'data-search-title="{display}"' in table
        assert f'<p class="ql-grid-title">{display}</p>' in grid
        assert f'aria-label="Download {display} by The Lab"' in table


def test_new_release_check_refused_without_baseline(
        client, monkeypatch, tmp_path):
    # "Check for new releases" is a library-walk-and-compare, useless until a
    # full library scan has built the baseline.
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import new_releases
    from qobuz_librarian.web import flows
    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(
        cfg,
        "LIBRARY_GENERATION_STATE_FILE",
        tmp_path / "generation.json",
    )
    monkeypatch.setattr(
        cfg,
        "NEW_RELEASE_STATE_FILE",
        tmp_path / "new-releases.json",
    )
    monkeypatch.setattr(flows, "scan_new_releases", lambda *a, **k: None)
    assert new_releases.is_baseline_complete() is False      # fresh state, no baseline
    r = client.post("/library", data={"mode": "new_releases"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/library")
    assert app_mod._active_new_release_check() is None       # no crawl was started


def test_library_scan_refusal_returns_to_settings(client, monkeypatch, tmp_path):
    # Settings' Force full rescan posts to the same /library route the
    # Library page uses. A refusal that starts nothing must send the user
    # back to Settings, where they clicked from, not bounce them onto
    # Library with an error about a page they weren't on.
    from qobuz_librarian import config as cfg
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path / "does-not-exist")

    r = client.post(
        "/library",
        data={"mode": "missing_albums", "force_full": "1",
              "return_to": "/settings"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/settings?error=")

    # Without return_to (the Library page's own launcher), the refusal still
    # lands back on Library as before.
    r = client.post(
        "/library",
        data={"mode": "missing_albums", "force_full": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/library?error=")


def test_lyrics_submission_never_checks_qobuz(client, monkeypatch):
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(
        webapp,
        "_authorize_qobuz_for_web",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Lyrics must not check Qobuz")
        ),
    )
    submitted = []
    monkeypatch.setattr(
        job_mgr,
        "submit",
        lambda job, _fn: submitted.append(job) or job,
    )

    response = client.post("/lyrics", follow_redirects=False)

    assert response.status_code == 303
    assert len(submitted) == 1
    assert submitted[0].execute_kind == "lyrics"


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/library", {"mode": "missing_albums"}),
        ("/library", {"mode": "new_releases"}),
        ("/repair", {}),
    ],
)
def test_remote_scan_preflight_precedes_job_admission(
        client, monkeypatch, path, data):
    from urllib.parse import unquote

    from qobuz_librarian.api.auth import QobuzUnavailable
    from qobuz_librarian.library import new_releases
    from qobuz_librarian.web import app as app_mod

    async def unavailable(*_args, **_kwargs):
        raise QobuzUnavailable("offline")

    admitted = []
    monkeypatch.setattr(app_mod, "_authorize_qobuz_for_web", unavailable)
    monkeypatch.setattr(
        app_mod,
        "_library_scan_state",
        lambda: {"ready": True, "count": 1, "message": ""},
    )
    monkeypatch.setattr(new_releases, "is_baseline_complete", lambda: True)
    monkeypatch.setattr(
        app_mod.job_mgr,
        "submit_scan",
        lambda *_args, **_kwargs: admitted.append(True),
    )

    response = client.post(path, data=data, follow_redirects=False)

    assert response.status_code == 303
    assert "Qobuz could not be reached" in unquote(response.headers["location"])
    assert "Nothing changed" in unquote(response.headers["location"])
    assert admitted == []




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

    ok, warnings = ss.save({"BEETS_PATH_DEFAULT": ""})
    assert ok is True
    assert cfg.BEETS_PATH_DEFAULT == "$albumartist/$album"
    assert "BEETS_PATH_DEFAULT" not in json.loads(store.read_text())
    # The box refills itself on the next render, so the save has to say why.
    assert any("BEETS_PATH_DEFAULT" in w for w in warnings)

    # A store already carrying a pinned blank recovers on the next start.
    store.write_text(json.dumps({"BEETS_PATH_DEFAULT": ""}))
    ss.load()
    assert cfg.BEETS_PATH_DEFAULT == "$albumartist/$album"
    assert "BEETS_PATH_DEFAULT" not in json.loads(store.read_text())


def test_worker_does_not_apply_settings_while_other_lane_runs(monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    active = jm.Job(id="active-lane", status=jm.JobStatus.RUNNING)
    waiting = jm.Job(id="waiting-lane")
    registry = jm.JobRegistry()
    registry.add(active)
    registry.add(waiting)
    seen = []

    class OneItemQueue:
        used = False

        def get(self, timeout):
            if self.used:
                raise SystemExit
            self.used = True
            return waiting, lambda _job: seen.append(
                (cfg.DOWNSAMPLE_HIRES_ENABLED, dict(ss._pending_apply))
            )

        @staticmethod
        def task_done():
            return None

    monkeypatch.setattr(jm, "registry", registry)
    monkeypatch.setattr(jm, "_stop_event", threading.Event())
    monkeypatch.setattr(jm.job_persistence, "admit", lambda _job: True)
    monkeypatch.setattr(jm.job_persistence, "persist", lambda _job: True)
    monkeypatch.setattr(jm, "_start_post_job_hook_for_job", lambda _job: None)
    monkeypatch.setattr(cfg, "DOWNSAMPLE_HIRES_ENABLED", False)
    monkeypatch.setattr(
        ss,
        "_pending_apply",
        {"DOWNSAMPLE_HIRES_ENABLED": True},
    )

    with pytest.raises(SystemExit):
        jm._worker_loop(OneItemQueue())

    assert waiting.status is jm.JobStatus.DONE
    assert seen == [(False, {"DOWNSAMPLE_HIRES_ENABLED": True})]
    assert cfg.DOWNSAMPLE_HIRES_ENABLED is False


def test_failed_settings_write_does_not_apply_live_values(monkeypatch):
    """A failed durable save must leave the running process unchanged too."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(cfg, "DOWNSAMPLE_HIRES_ENABLED", False)
    monkeypatch.setattr(ss, "_any_active_job", lambda: False)
    monkeypatch.setattr(ss, "_atomic_write_settings", lambda _data: False)
    with ss._pending_lock:
        ss._pending_apply = None

    ok, _ = ss.save({"DOWNSAMPLE_HIRES_ENABLED": True})

    assert ok is False
    assert cfg.DOWNSAMPLE_HIRES_ENABLED is False
    with ss._pending_lock:
        assert ss._pending_apply is None




def test_quality_change_flags_the_stale_upgrade_review(client, tmp_path, monkeypatch):
    """Lowering/raising the download quality leaves a saved Upgrade
    review promising dead targets, so the save must say a refresh updates it.
    An unchanged save stays quiet."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 4)
    monkeypatch.setattr(cfg, "PREFER_HIRES", True)
    monkeypatch.setattr(ss, "_any_active_job", lambda: False)
    state = {
        "complete": True,
        "quality_signature": upgrade_state.quality_signature(),
        "candidates": [
            {
                "title": "Dummy",
                "artist": "Portishead",
                "detail": "16-bit/44.1 kHz -> 24-bit/192 kHz",
                "payload": {"album_id": "up1"},
            }
        ],
    }
    _make_saved_surface_current(monkeypatch, "upgrade", state)
    monkeypatch.setattr(upgrade_state, "load", lambda: state)
    monkeypatch.setattr(ss, "_pending_apply", None)

    r = client.post("/settings/behavior", data={"STREAMRIP_QUALITY": "2"}, follow_redirects=False)
    assert r.status_code == 303
    assert "quality_note=1" in r.headers["location"]
    assert r.headers["location"].endswith("#behaviour")
    r2 = client.post("/settings/behavior", data={"STREAMRIP_QUALITY": "2"}, follow_redirects=False)
    assert "quality_note" not in r2.headers["location"]
    assert r2.headers["location"].endswith("#behaviour")

    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 4)
    monkeypatch.setattr(ss, "_any_active_job", lambda: True)
    with ss._pending_lock:
        ss._pending_apply = None
    review = webapp._review_job_from_upgrade_state(state)
    review.candidates[0]["selected"] = True
    monkeypatch.setattr(webapp, "_upgrade_available", lambda *_a, **_k: True)

    deferred = client.post(
        "/settings/behavior",
        data={"STREAMRIP_QUALITY": "2"},
        follow_redirects=False,
    )

    assert "queued=1" in deferred.headers["location"]
    assert "quality_note=1" in deferred.headers["location"]
    assert cfg.STREAMRIP_QUALITY == 4
    assert ss.current()["STREAMRIP_QUALITY"] == "2"
    refused = client.post(f"/jobs/{review.id}/approve", follow_redirects=False)

    assert refused.status_code == 303
    assert "Download%20quality%20changed" in refused.headers["location"]
    assert review.status == job_mgr.JobStatus.AWAITING_REVIEW


def test_settings_save_only_pins_changed_fields(tmp_path, monkeypatch):
    """Saving the Settings form must not freeze untouched fields into the
    settings file: the file wins over env on load, so writing a field that
    merely matched its current value would silently stop that env var from
    ever applying again. Only real changes (and fields saved before) persist."""
    import json

    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(cfg, "LYRICS_ENABLED", True)
    monkeypatch.setattr(cfg, "PREFER_HIRES", True)
    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 4)
    monkeypatch.setattr(cfg, "DOWNSAMPLE_KEEP_ORIGINALS", None)
    monkeypatch.setattr(ss, "_any_active_job", lambda: False)
    with ss._pending_lock:
        ss._pending_apply = None

    # The form posts every field; only LYRICS_ENABLED actually changed.
    ok, _ = ss.save({"LYRICS_ENABLED": False, "PREFER_HIRES": True,
                     "STREAMRIP_QUALITY": "4",
                     "DOWNSAMPLE_KEEP_ORIGINALS": ""})
    assert ok is True
    on_disk = json.loads((tmp_path / "s.json").read_text())
    assert on_disk == {"LYRICS_ENABLED": False}

    # A field that was saved before stays in the file even when a later save
    # posts it unchanged; the user set it deliberately, so it keeps winning.
    ok, _ = ss.save({"LYRICS_ENABLED": False, "PREFER_HIRES": False})
    on_disk = json.loads((tmp_path / "s.json").read_text())
    assert on_disk == {"LYRICS_ENABLED": False, "PREFER_HIRES": False}


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


# ── CSRF middleware ───────────────────────────────────────────────────────────




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
            assert "pid 4321" not in r.text
            assert "run-lock" not in r.text
            assert ">Try again</button>" in r.text
            assert ">Back to Search</a>" in r.text


def test_folder_move_recovery_pause_names_cause_and_exact_paths(
        client, monkeypatch, tmp_path, caplog):
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

    with caplog.at_level("WARNING", logger="qobuz_librarian"):
        blocked = client.post(
            "/download", data={"album_id": "1"}, follow_redirects=False
        )
    assert "exact relocation evidence changed" in caplog.text

    assert blocked.status_code == 503
    assert all(str(path) in blocked.text for path in affected_paths)
    assert "interrupted download" not in blocked.text
    # The internal reason is a str(exc) from the relocation code. It goes to the
    # log the message points at, not onto the screen.
    assert "exact relocation evidence changed" not in blocked.text












def test_upgrade_disabled_page_redirects_cleanly(client, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", False, raising=False)
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})

    r = client.get("/upgrade", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_upgrade_stays_reachable_without_qobuz_credentials(client, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", True, raising=False)
    monkeypatch.setattr(webapp, "_read_creds", lambda: {})

    response = client.get("/upgrade", follow_redirects=False)

    assert response.status_code == 200
    assert 'href="/upgrade"' in response.text
    assert "Connect Qobuz" in response.text


def test_saved_upgrade_review_opens_without_contacting_qobuz(client, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import app as webapp

    review = jm.Job(title="Upgrade review", status=jm.JobStatus.AWAITING_REVIEW)
    review.execute_kind = "upgrade"

    async def unexpected_auth(*_args, **_kwargs):
        raise AssertionError("opening saved Upgrade state must stay local")

    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", True, raising=False)
    monkeypatch.setattr(webapp, "_read_creds", lambda: {})
    monkeypatch.setattr(webapp, "_authorize_qobuz_for_web", unexpected_auth)
    monkeypatch.setattr(
        webapp,
        "_review_job_from_current_saved_state",
        lambda _kind: review,
    )

    response = client.post("/upgrade/review", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/jobs/{review.id}"


@pytest.mark.parametrize(
    ("execute_kind", "path"),
    [
        ("library", "/library"),
        ("new_releases", None),
        ("upgrade", None),
        ("repair", "/repair"),
    ],
)
def test_saved_remote_reviews_remain_visible_without_qobuz(
        client, monkeypatch, execute_kind, path):
    from qobuz_librarian.web import app as webapp

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
    monkeypatch.setattr(webapp, "_read_creds", lambda: {})
    monkeypatch.setattr(webapp, "_qobuz_ready", lambda: False)
    destination = path or f"/jobs/{job.id}"
    try:
        response = client.get(destination)

        assert response.status_code == 200
        assert "Saved Album" in response.text
        assert 'data-review-blocked="1"' in response.text
        assert 'id="review-submit"' in response.text
    finally:
        _remove_job(job)


def test_stale_library_snapshot_rebuilds_review_and_offers_refresh(
        client, monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import (
        generation_state,
        library_scan_state,
    )
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(
        cfg,
        "LIBRARY_GENERATION_STATE_FILE",
        tmp_path / "generation.json",
    )
    monkeypatch.setattr(
        cfg,
        "LIBRARY_SCAN_STATE_FILE",
        tmp_path / "library.json",
    )
    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    attempt = generation_state.begin_attempt()
    publication = generation_state.commit_catalog_generation(attempt)
    revision = generation_state.reserve_revision()
    assert library_scan_state.save_kind(
        "missing",
        artists={
            "Saved Artist": {
                "fingerprint": "fp",
                "artist_id": "artist-1",
                "catalog_ids": ["album-1"],
                "candidates": [{
                    "kind": "album",
                    "title": "Saved Album",
                    "artist": "Saved Artist",
                    "detail": "2026",
                    "payload": {"album_id": "album-1"},
                }],
            },
        },
        complete=True,
        generation=publication["generation"],
        revision=revision,
    )
    reason = "The changed album could not be tied to one Library result."
    assert generation_state.invalidate(["library"], reason)
    assert generation_state.library_snapshot_available()
    assert generation_state.baseline_complete() is False
    monkeypatch.setattr(webapp, "_qobuz_ready", lambda: True)
    monkeypatch.setattr(
        webapp,
        "_read_creds",
        lambda: {"auth_token": "saved-token", "user_id": "saved-user"},
    )
    monkeypatch.setattr(
        webapp,
        "_library_scan_state",
        lambda: {"ready": True, "count": 1, "message": ""},
    )

    response = client.get("/library")

    assert response.status_code == 200
    assert "Saved Album" in response.text
    assert "Refresh needed" in response.text
    assert reason in response.text
    assert 'aria-label="Scan for music added outside the app"' in response.text
    assert "ql-scan-hero-meta" not in response.text


def test_stale_new_release_review_names_its_saved_status(
        client, monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import generation_state

    monkeypatch.setattr(
        cfg,
        "LIBRARY_GENERATION_STATE_FILE",
        tmp_path / "generation.json",
    )
    attempt = generation_state.begin_attempt()
    publication = generation_state.commit_catalog_generation(attempt)
    reason = "The changed album could not be tied to one New Releases result."
    assert generation_state.mark_output_status(
        "new_releases",
        "stale",
        generation=publication["generation"],
        reason=reason,
    )
    job = jm.Job(title="New-release check")
    job.execute_kind = "new_releases"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job.add_candidate(
        "album",
        "Saved New Release",
        "Saved Artist",
        payload={"album_id": "new-1"},
    )
    jm.registry.add(job)
    try:
        response = client.get(f"/jobs/{job.id}")

        assert response.status_code == 200
        assert "Saved New Release" in response.text
        assert "Refresh needed" in response.text
        assert reason in response.text
        assert 'href="/library"' in response.text
    finally:
        _remove_job(job)








def test_saved_review_creation_is_atomic_for_parallel_posts(monkeypatch):
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(webapp, "_web_writes_paused", lambda: False)
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
    _make_saved_surface_current(monkeypatch, "upgrade", state)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        jobs = list(
            ex.map(
                lambda _i: webapp._review_job_from_upgrade_state(state),
                range(8),
            )
        )

    assert len({j.id for j in jobs}) == 1
    assert len([j for j in job_mgr.registry.awaiting_review() if j.execute_kind == "upgrade"]) == 1

    for candidate in jobs[0].candidates:
        candidate["selected"] = True
    jobs[0].status = job_mgr.JobStatus.RUNNING
    remaining = {
        "complete": True,
        "candidates": [
            {
                **state["candidates"][1],
                "detail": "fresh saved-state detail",
            }
        ],
    }
    claimed = webapp._review_job_from_upgrade_state(remaining)
    assert claimed is jobs[0]
    assert (
        len(
            [
                j
                for j in job_mgr.registry.all()
                if j.execute_kind == "upgrade" and j.status in job_mgr.ACTIVE
            ]
        )
        == 1
    )


def test_upgrade_saved_review_respects_hidden_candidates(
        client, monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import job_persistence
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    monkeypatch.setattr(job_persistence, "_persist_locked", lambda _job: True)
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    state = {
            "updated_at": time.time(),
            "complete": True,
            "quality_signature": webapp._effective_upgrade_quality_signature(),
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
    assert "1 upgrade candidate" in r.text
    assert "2 upgrade candidates" not in r.text

    second = client.post("/upgrade/review", follow_redirects=False)
    assert second.headers["location"] == first.headers["location"]
    assert len([
        j for j in job_mgr.registry.awaiting_review()
        if j.execute_kind == "upgrade"
    ]) == 1




def test_upgrade_approve_refuses_changed_saved_state_without_mutating_review(
        client, monkeypatch):
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import job_persistence
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(
        "qobuz_librarian.library.candidate_premise.validate_all",
        lambda _candidates: [],
    )

    monkeypatch.setattr(job_persistence, "_persist_locked", lambda _job: True)
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(webapp, "_read_creds", lambda: {"auth_token": "dummy", "user_id": "dummy"})
    state = {
        "updated_at": time.time(),
        "complete": True,
        "quality_signature": webapp._effective_upgrade_quality_signature(),
        "candidates": [
            {
                "title": "Stale",
                "artist": "Portishead",
                "detail": "16-bit/44.1 kHz -> 24-bit/96 kHz",
                "payload": {"album_id": "old", "year": "1994", "cover": ""},
            }
        ],
    }
    _make_saved_surface_current(monkeypatch, "upgrade", state)
    monkeypatch.setattr("qobuz_librarian.quality.upgrade_state.load", lambda: state)

    first = client.post("/upgrade/review", follow_redirects=False)
    job_id = first.headers["location"].removeprefix("/jobs/")
    job = job_mgr.registry.get(job_id)
    client.post(f"/jobs/{job.id}/select", data={"cid": job.candidates[0]["cid"], "checked": "1"})
    state["candidates"] = []

    r = client.post(f"/jobs/{job.id}/approve", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"].startswith(f"/jobs/{job.id}?error=")
    assert job.status == job_mgr.JobStatus.AWAITING_REVIEW
    assert [candidate["title"] for candidate in job.candidates] == ["Stale"]
    assert job.candidates[0]["selected"] is True




def test_approve_refuses_parked_library_review_without_credentials(
        client, monkeypatch):
    from qobuz_librarian.api.auth import NoCredsError
    from qobuz_librarian.web import app as webapp

    async def no_credentials(*_args, **_kwargs):
        raise NoCredsError()

    monkeypatch.setattr(webapp, "_read_creds", lambda: {})
    monkeypatch.setattr(webapp, "_authorize_qobuz_for_web", no_credentials)
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


@pytest.mark.parametrize(
    "execute_kind",
    ["library", "new_releases", "upgrade", "repair"],
)
def test_qobuz_approval_failure_preserves_the_exact_review(
        client, monkeypatch, execute_kind):
    from qobuz_librarian.api.auth import AuthLost
    from qobuz_librarian.web import app as webapp

    async def rejected(*_args, **_kwargs):
        raise AuthLost("rejected")

    job = jm.Job(title="Saved review")
    job.kind = "scan"
    job.execute_kind = execute_kind
    job.status = jm.JobStatus.AWAITING_REVIEW
    job._execute_fn = lambda _job, _chosen: None
    job.execute_args = {
        "quality_signature": webapp._effective_upgrade_quality_signature(),
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
    monkeypatch.setattr(webapp, "_authorize_qobuz_for_web", rejected)
    monkeypatch.setattr(
        webapp,
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
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import flows

    monkeypatch.setattr(
        "qobuz_librarian.library.candidate_premise.validate_all",
        lambda _candidates: [],
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
        assert _wait_for(lambda: any(
            "untouched" in line for line in job.log_lines))
        assert _wait_for(lambda: job.status == jm.JobStatus.AWAITING_REVIEW)
        assert job.candidates[0]["selected"]
        assert job.finished_at is None
        assert job.error is None
    finally:
        _remove_job(job)


def test_auth_failure_before_import_rejoins_a_split_review(monkeypatch):
    """A pre-mutation failure restores one review, not two partial reviews."""
    from qobuz_librarian.api.auth import AuthLost
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import job_persistence

    restored = []

    def restore(main, remnant):
        restored.append((main.id, remnant.id))
        return True

    monkeypatch.setattr(job_persistence, "restore_split_review", restore)

    job = jm.Job(title="Library split recovery")
    job.kind = "scan"
    job.execute_kind = "library"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job.add_candidate(
        "album", "Picked", "Artist",
        payload={"album_id": "picked"}, selected=True,
    )
    job.add_candidate(
        "album", "Left parked", "Artist",
        payload={"album_id": "parked"}, selected=False,
    )

    def dies(_job, _chosen):
        raise AuthLost("token rejected")

    split = []

    def split_review(review):
        remnant = webapp._build_unapproved_review(review, "")
        split.append(remnant)
        return remnant

    job._execute_fn = dies
    jm.registry.add(job)
    try:
        jm.start_worker()
        assert jm.approve(job, None, split_review=split_review) is True
        assert _wait_for(
            lambda: job.status == jm.JobStatus.AWAITING_REVIEW
        ), (job.status, job.error, job.log_lines)
        assert [(candidate["title"], candidate["selected"])
                for candidate in job.candidates] == [
            ("Picked", True),
            ("Left parked", False),
        ]
        assert split and jm.registry.get(split[0].id) is None
        assert restored == [(job.id, split[0].id)]
    finally:
        _remove_job(job)
        if split and split[0] is not None:
            _remove_job(split[0])


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


def test_downsample_confirmation_matches_a_deferred_delete_policy(
        client, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import jobs as job_mgr
    from qobuz_librarian.web import settings_store as ss

    _allow_legacy_candidate_execution(monkeypatch)

    # A review that promised retained originals must not silently switch to a
    # deferred delete policy saved in another tab before the approval lands.
    monkeypatch.setattr(cfg, "DOWNSAMPLE_KEEP_ORIGINALS", "keep")
    monkeypatch.setattr(ss, "_pending_apply", None)
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE",
        True,
    )
    monkeypatch.setattr(job_mgr._scan_queue, "put", lambda _item: None)
    job = job_mgr.Job(
        title="Deferred policy review",
        kind="scan",
        execute_kind="downsample",
        review_verb="Downsample",
        status=job_mgr.JobStatus.AWAITING_REVIEW,
    )
    job._execute_fn = lambda _job, _chosen: None
    job.add_candidate(
        "downsample",
        "Album",
        "Artist",
        payload={"album_dir": "/music/Artist/Album"},
        selected=True,
    )
    job_mgr.registry.add(job)
    try:
        page = client.get(f"/jobs/{job.id}")
        assert 'name="downsample_policy" value="keep"' in page.text

        monkeypatch.setattr(
            ss,
            "_pending_apply",
            {"DOWNSAMPLE_KEEP_ORIGINALS": "delete"},
        )
        stale = client.post(
            f"/jobs/{job.id}/approve",
            data={"downsample_policy": "keep"},
            follow_redirects=False,
        )

        assert stale.status_code == 303
        assert "error=" in stale.headers["location"]
        assert job.status == job_mgr.JobStatus.AWAITING_REVIEW
        assert "keep_originals" not in job.execute_args

        updated = client.get(f"/jobs/{job.id}")
        assert 'name="downsample_policy" value="delete"' in updated.text
        assert "data-irreversible" in updated.text

        response = client.post(
            f"/jobs/{job.id}/approve",
            data={"downsample_policy": "delete"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert job.execute_args["keep_originals"] is False
    finally:
        _remove_job(job)




def test_approve_refuses_parked_downsample_review_without_engine(
        client, monkeypatch):
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    state = {
                "updated_at": time.time(),
                "complete": True,
                "candidates": [{
                "title": "Dummy",
                "artist": "Portishead",
                "detail": "24-bit / 96 kHz -> 16-bit / 48 kHz",
                "album_dir": "/music/Portishead/Dummy",
                    "est_saving": 1234,
                }],
            }
    _make_saved_surface_current(monkeypatch, "downsample", state)
    monkeypatch.setattr(
        "qobuz_librarian.library.downsample_state.load", lambda: state)

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




def test_dashboard_first_run_offers_baseline_scan_with_skip(client, monkeypatch):
    # On first run the dashboard OFFERS the baseline scan (Scan / Skip) rather
    # than auto-starting it.
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import generation_state, new_releases
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr("qobuz_librarian.library.scanner.list_library_artists",
                        lambda: ["Some Artist"])
    monkeypatch.setattr(cfg, "AUTO_LIBRARY_SCAN", True)
    monkeypatch.setattr(new_releases, "is_baseline_complete", lambda: False)
    monkeypatch.setattr(generation_state, "baseline_complete", lambda: False)
    monkeypatch.setattr(new_releases, "auto_scan_attempted", lambda: False)

    r = client.get("/")

    assert r.status_code == 200
    # The scan form only renders as an offer; a running scan replaces it.
    assert 'name="mode" value="missing_albums"' in r.text
    assert 'action="/library/skip-setup"' in r.text












def test_retry_rebuilds_archived_failed_download(client, monkeypatch):
    from qobuz_librarian.api.auth import QobuzUnavailable
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import job_persistence

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

    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
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

    monkeypatch.setattr(webapp, "_make_single_track_run", single_run)

    jobs_before = {item.id for item in jm.registry.all()}
    r = client.post(f"/jobs/{archived.id}/retry", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"].startswith("/queue?error=")
    assert (
        "Qobuz is temporarily unavailable (network or rate limit). "
        "Try again shortly."
    ) in client.get(r.headers["location"]).text
    assert job_persistence.load_one(archived.id)["status"] == "failed"
    assert {item.id for item in jm.registry.all()} == jobs_before

    outage["active"] = False
    r = client.post(f"/jobs/{archived.id}/retry", follow_redirects=False)

    assert r.status_code == 303
    new_id = r.headers["location"].removeprefix("/jobs/")
    assert new_id and new_id != archived.id
    new_job = jm.registry.get(new_id)
    assert new_job is not None and new_job.album_id == "al1"
    assert seen == {"track_id": "roads-live"}
    assert new_job.single == {"album_id": "al1", "track_id": "roads-live"}
    assert new_job.edition == "Live Version"
    assert new_job.display_title == "Roads (Live Version)"
    assert job_persistence.load_one(new_id)["edition"] == "Live Version"
    assert "Roads (Live Version)" in client.get("/queue/history").text
    _remove_job(new_job)


def test_retry_keeps_the_new_edition_override(client, monkeypatch):
    # "Download this edition anyway" lives on the job (execute_args), not just
    # in the run closure; a retried edition download that lost the flag would
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


def test_retry_finishes_a_download_whose_settlement_refused_after_clearing_it(
    client, monkeypatch,
):
    """Retry after a failed staging cleanup."""
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()

    job = jm.Job(title="Anvil Vapre", artist="Autechre", album_id="al-anvil")
    job.status = jm.JobStatus.FAILED
    job.attention = "recovery"
    job.finished_at = time.time() - 5
    jm.registry.add(job)
    job_persistence.persist(job)

    settled = {"done": False}

    def _record(_authority):
        status = (
            StartupRecoveryStatus.CLEAR
            if settled["done"]
            else StartupRecoveryStatus.ATTENTION_REQUIRED
        )
        result = StartupRecoveryResult(status)
        webapp._STARTUP_RECOVERY_RESULT = result
        return result

    def _settle(_job, _action):
        settled["done"] = True
        return False, ("Beets may have started or changed the library, so this "
                       "item remains blocked.")

    monkeypatch.setattr(webapp, "_record_startup_recovery", _record)
    monkeypatch.setattr(webapp, "_settle_durable_web_recovery", _settle)
    monkeypatch.setattr(webapp, "_durable_recovery_matches_job", lambda j: True)
    monkeypatch.setattr(webapp, "_recovery_submission_matches",
                        lambda j, op, item: True)
    monkeypatch.setattr(webapp, "_durable_completion_status",
                        lambda j: settled["done"])

    r = client.post(
        f"/jobs/{job.id}/retry",
        data={"recovery_operation_id": "op-1", "recovery_item_id": "item-1"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert r.headers["location"] == f"/jobs/{job.id}"
    assert job.status is jm.JobStatus.DONE
    assert job.attention == ""
    assert "restart" not in job.summary
    _remove_job(job)


def test_giving_up_refuses_a_download_a_web_job_still_owns(client, monkeypatch):
    """Give up exists for a terminal download, which has no job page to settle
    it from. Letting it settle a job-owned recovery would settle it behind that
    job's back, leaving its own record still saying the download is blocked."""
    from types import SimpleNamespace

    from qobuz_librarian.queue import startup_recovery
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )
    from qobuz_librarian.web import app as webapp

    def _record(_authority):
        result = StartupRecoveryResult(StartupRecoveryStatus.ATTENTION_REQUIRED)
        webapp._STARTUP_RECOVERY_RESULT = result
        return result

    settled = []
    monkeypatch.setattr(webapp, "_record_startup_recovery", _record)
    monkeypatch.setattr(webapp, "_run_lock_intact", lambda: True)
    monkeypatch.setattr(webapp, "_terminal_recovery_offer", lambda: {
        "operation_id": "op-1", "item_id": "item-1", "album": "Autechre - Amber",
    })
    monkeypatch.setattr(webapp, "_startup_recovery_web_job_id", lambda: "job-7")
    monkeypatch.setattr(webapp, "_startup_recovery_binding", lambda: (
        SimpleNamespace(operation_id="op-1", item_id="item-1"), None, None, None,
    ))
    def _settle(**kwargs):
        settled.append(kwargs)
        return SimpleNamespace(status=None, reason="")

    monkeypatch.setattr(startup_recovery, "settle_blocked_item", _settle)

    r = client.post(
        "/queue/interrupted/discard",
        data={"recovery_operation_id": "op-1", "recovery_item_id": "item-1"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert settled == []


def test_undo_keeps_failed_catalog_cleanup_retryable_in_the_archive(
        client, monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets as beets_mod
    from qobuz_librarian.integrations.beets import ForgetBeetsEntriesResult
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import flows, job_persistence

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path)
    monkeypatch.setattr(
        flows, "_refresh_after_local_album_change", lambda *args, **kwargs: None
    )

    album = tmp_path / "Portishead" / "Dummy"
    album.mkdir(parents=True)
    track = album / "05 - Glory Box.flac"
    track.write_bytes(b"audio")
    owned = webapp._bind_owned_path(tmp_path, track)
    assert owned is not None

    outcomes = iter([
        ForgetBeetsEntriesResult(False),
        ForgetBeetsEntriesResult(True, 1),
    ])
    monkeypatch.setattr(
        beets_mod, "forget_beets_entries", lambda _paths: next(outcomes)
    )

    job = jm.Job(title="Dummy", artist="Portishead")
    job.status = jm.JobStatus.DONE
    job.single = {
        "dir": str(album),
        "track_id": "t1",
        "title": "Glory Box",
        "owned_root": str(tmp_path),
        "owned_path": owned,
    }
    job.finished_at = time.time()
    job_persistence.persist(job)

    r = client.post(f"/jobs/{job.id}/undo", follow_redirects=False)

    assert r.status_code == 303
    assert not track.exists()
    row = job_persistence.load_one(job.id)
    assert row is not None
    assert row["single"].get("removed") is True
    assert row["single"]["catalog_cleanup"]["pending"] is True
    assert row["attention"] == "catalog"

    job_persistence.clear_history()
    assert job_persistence.load_one(job.id) is not None
    page = client.get(f"/jobs/{job.id}")
    assert page.status_code == 200
    assert job_persistence.load_one(job.id)["attention"] == "catalog"

    r = client.post(f"/jobs/{job.id}/undo", follow_redirects=False)

    assert r.status_code == 303
    row = job_persistence.load_one(job.id)
    assert row["single"].get("catalog_cleanup") is None
    assert row["attention"] == ""
    job_persistence.clear_history()
    assert job_persistence.load_one(job.id) is None


def test_undo_bounces_when_the_staging_mutex_is_held(client, monkeypatch, tmp_path):
    """Undo behind a long staging-lock holder (library-wide Lyrics scan,
    migration) must bounce naming the holder instead of hanging the request
    until the holder finishes; the DONE job page can't show progress, so a
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




def test_queue_cancel_stays_on_queue_without_accepting_other_targets(client):
    running = _inject_job(jm.JobStatus.RUNNING, "Running library scan")
    running.execute_kind = "library"
    other = _inject_job(jm.JobStatus.RUNNING, "Another library scan")
    other.execute_kind = "library"
    try:
        queue = client.get("/queue")
        assert 'name="return_to" value="/queue"' in queue.text

        canceled = client.post(
            f"/jobs/{running.id}/cancel",
            data={"return_to": "/queue"},
            follow_redirects=False,
        )
        assert canceled.status_code == 303
        assert canceled.headers["location"] == "/queue"
        assert running.cancel_requested is True

        untrusted = client.post(
            f"/jobs/{other.id}/cancel",
            data={"return_to": "https://example.invalid/leave"},
            follow_redirects=False,
        )
        assert untrusted.status_code == 303
        assert untrusted.headers["location"] == "/library"
        assert other.cancel_requested is True

        other.status = jm.JobStatus.DONE
        stale = client.post(
            f"/jobs/{other.id}/cancel",
            data={"return_to": "/queue"},
            follow_redirects=False,
        )
        assert stale.status_code == 303
        assert stale.headers["location"].startswith("/queue?error=")
    finally:
        _remove_job(running)
        _remove_job(other)




def test_library_job_page_redirects_to_library(client):
    """A library-kind job has no page of its own: /library is the only
    Library review surface, so an old link or a History card must land the
    user there, tab and filter carried over, instead of opening a second
    review at /jobs/{id}."""
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    try:
        r = client.get(f"/jobs/{job.id}", params={"tab": "gaps", "q": "Dum"},
                       follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/library")
        assert "tab=gaps" in r.headers["location"]
        assert "q=Dum" in r.headers["location"]
    finally:
        _remove_job(job)


def test_library_hide_scoped_to_review_tab(client, monkeypatch, tmp_path):
    """A library review with both missing albums and Gap Fill splits into tabs,
    and dismissing an artist's unselected rows from one tab must not silently
    drop that artist's candidates on the other tab."""
    from qobuz_librarian.library import hidden
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", tmp_path / "h.json")
    monkeypatch.setattr(job_persistence, "_persist_locked", lambda _job: True)

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=False)
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      detail="1994 · CD 16-bit/44.1kHz · gap-fill: 2 missing of 11",
                      payload={"year": "1994", "gap_fill": 2}, selected=False)
    try:
        r = client.get("/library")
        assert r.status_code == 200
        assert "Missing Albums" in r.text and "Gap Fill" in r.text
        # The default tab shows only the missing album, not the gap fill row.
        assert "Third" in r.text and "Dummy" not in r.text
        restored = client.get(
            "/library", params={"tab": "gaps", "q": "Dum"}
        )
        assert restored.status_code == 200
        assert "Dummy" in restored.text and "Third" not in restored.text
        assert 'id="review-filter" autocomplete="off"\n             value="Dum"' in restored.text
        r = client.get(f"/jobs/{job.id}/review", params={"tab": "gaps"},
                       headers={"HX-Request": "true"})
        assert "Dummy" in r.text and "Third" not in r.text
        assert 'data-review-total="2"' in r.text
        assert 'data-review-missing-total="1"' in r.text
        assert 'data-review-gap-total="1"' in r.text

        r = client.post(f"/jobs/{job.id}/hide",
                        data={"artist": "Portishead", "tab": "missing"})
        assert r.status_code == 200
        assert [c["title"] for c in job.candidates] == ["Dummy"]
        store = hidden.load()
        assert hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Third", store)
        assert not hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Dummy", store)
    finally:
        _remove_job(job)


@pytest.mark.parametrize(
    "summary_suffix",
    [".", ", from your last library scan."],
)
def test_review_hides_only_the_duplicated_count_summary(
        client, summary_suffix):
    from qobuz_librarian.web import flows

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.add_candidate(
        kind="album",
        title="Third",
        artist="Portishead",
        payload={"year": "2008"},
    )
    job.add_candidate(
        kind="album",
        title="Dummy",
        artist="Portishead",
        detail="gap-fill: 2 missing of 11",
        payload={"year": "1994", "gap_fill": 2},
    )
    count_summary = flows.library_review_summary(job.candidates) + summary_suffix
    job.summary = count_summary
    try:
        embedded = client.get(f"/jobs/{job.id}/content?embedded=1")
        assert embedded.status_code == 200
        assert count_summary not in embedded.text
        assert "Missing Albums" in embedded.text
        assert "Gap Fill" in embedded.text

        job.candidates.pop()
        stale = client.get(f"/jobs/{job.id}/content?embedded=1")
        assert stale.status_code == 200
        assert count_summary not in stale.text

        full_page = client.get(f"/jobs/{job.id}")
        assert full_page.status_code == 200
        assert count_summary not in full_page.text

        job.summary += " 2 artists couldn't be checked; scan again to finish."
        caveat = client.get(f"/jobs/{job.id}/content?embedded=1")
        assert count_summary in caveat.text
        assert "scan again to finish" in caveat.text
    finally:
        _remove_job(job)


def test_stale_whole_album_repair_is_refused_before_qobuz(
        client, monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library.candidate_premise import capture
    from qobuz_librarian.web import app as webapp

    music = tmp_path / "music"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    track = album / "01.flac"
    track.write_bytes(b"reviewed bytes")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    premise = capture("repair-redownload", album)
    assert premise is not None

    job = jm.Job(title="Repair scan")
    job.kind = "scan"
    job.execute_kind = "repair"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job._execute_fn = lambda _job, _chosen: None
    job.add_candidate(
        "redownload",
        "Album",
        "Artist",
        payload={
            "album_id": "a1",
            "album_dir": str(album),
            "artist_name": "Artist",
            "_premise": premise,
        },
        selected=True,
    )
    jm.registry.add(job)
    before = copy.deepcopy((job.candidates, job.execute_args, job.status))

    async def unexpected_qobuz(*_args, **_kwargs):
        raise AssertionError("stale local work must fail before Qobuz")

    monkeypatch.setattr(webapp, "_authorize_qobuz_for_web", unexpected_qobuz)
    track.write_bytes(b"different bytes after review")
    try:
        response = client.post(
            f"/jobs/{job.id}/approve",
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"].startswith("/repair?error=")
        assert (job.candidates, job.execute_args, job.status) == before
    finally:
        _remove_job(job)


def test_history_job_cards_reach_past_the_first_page(client, monkeypatch):
    """The job cards are capped per page while the count above them reports the
    whole archive, so every page past the first has to be reachable.
    """
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import job_persistence

    cap = webapp._HISTORY_BULK_CAP
    made = [{"id": f"j{i:03d}", "title": f"Scan {i}", "artist": "",
             "status": "done", "kind": "scan", "execute_kind": "repair",
             "summary": "", "error": None, "album_id": "", "attention": "",
             "recoveries": [], "when": "", "when_exact": "",
             "created_at": float(i), "finished_at": float(i)}
            for i in range(cap + 5)]
    monkeypatch.setattr(job_persistence, "recovery_history", lambda: [])
    monkeypatch.setattr(job_persistence, "history_count",
                        lambda **kw: len(made) if kw.get("bulk") else 0)
    monkeypatch.setattr(
        job_persistence, "history_page",
        lambda limit, offset, **kw: made[offset:offset + limit] if kw.get("bulk") else [])

    first = client.get("/queue/history")
    assert first.status_code == 200
    assert 'aria-label="Job pages"' in first.text
    assert "Scan 0" in first.text and f"Scan {cap + 4}" not in first.text

    second = client.get("/queue/history", params={"jp": 2})
    assert second.status_code == 200
    assert f"Scan {cap + 4}" in second.text




def test_archive_pruning_keeps_active_single_undo(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    job_persistence._reset_for_tests()
    job_persistence.init()

    protected = jm.Job(title="Protected Undo", artist="Portishead")
    protected.status = jm.JobStatus.DONE
    protected.finished_at = 1.0
    protected.single = {
        "dir": "/inert/album",
        "owned_path": {"relative": "05 - Glory Box.flac"},
    }
    oldest = jm.Job(title="Old history")
    oldest.status = jm.JobStatus.DONE
    oldest.finished_at = 2.0
    newest = jm.Job(title="New history")
    newest.status = jm.JobStatus.DONE
    newest.finished_at = 3.0
    for job in (protected, oldest, newest):
        assert job_persistence.persist(job)

    job_persistence.prune_finished(1)

    assert job_persistence.load_one(protected.id) is not None
    assert job_persistence.load_one(oldest.id) is None
    assert job_persistence.load_one(newest.id) is not None




def test_library_approve_scoped_to_tab_splits_off_other_tab(client, monkeypatch):
    """Downloading from one tab must consume only that tab: the other tab's
    candidates (and their saved ticks) split into their own parked review
    instead of dying with the executing job."""
    from qobuz_librarian.web import app as webapp
    monkeypatch.setattr(
        "qobuz_librarian.library.candidate_premise.validate_all",
        lambda _candidates: [],
    )
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
        # History lists a parked review by its summary, and the split used to
        # carry none, so the row arrived with nothing in it.
        assert split.summary
        assert split._execute_fn is not None
    finally:
        _remove_job(job)
        if split is not None:
            _remove_job(split)


def test_search_download_prunes_parked_library_review(client, monkeypatch, tmp_path):
    """A Search download that imports an album must drop that album from a
    parked library review; otherwise the stale review offers to download it
    again. Other candidates and their ticks stay put."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import review_badges
    monkeypatch.setattr(
        cfg, "REVIEW_BADGE_STATE_FILE", tmp_path / "review-badges.json")
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
    review_badges.mark_ready("library", now=100.0)
    runner = _inject_job(jm.JobStatus.RUNNING)
    second_runner = None
    try:
        album = {"id": "q123", "title": "Third",
                 "artist": {"name": "Portishead"}}
        webapp._make_download_run(album, token="tok")(runner)
        assert runner.status != jm.JobStatus.FAILED
        flags = {c["title"]: c["selected"] for c in parked.candidates}
        assert flags == {"Dummy": True}
        assert parked.status == jm.JobStatus.AWAITING_REVIEW
        assert review_badges.snapshot()["library"] is True

        second_runner = _inject_job(jm.JobStatus.RUNNING)
        album = {"id": "q456", "title": "Dummy",
                 "artist": {"name": "Portishead"}}
        webapp._make_download_run(album, token="tok")(second_runner)
        assert parked.status == jm.JobStatus.DONE
        assert review_badges.snapshot()["library"] is False
    finally:
        _remove_job(parked)
        _remove_job(runner)
        if second_runner is not None:
            _remove_job(second_runner)


def test_library_approve_skips_candidates_already_on_disk(client, monkeypatch):
    """Approving a parked review re-checks the disk: a missing-album candidate
    whose folder appeared while the review sat parked is dropped (and counted
    in the redirect note) instead of downloaded again. Gap Fill candidates are
    exempt: their folder exists by definition."""
    from qobuz_librarian.web import (
        app as webapp,
    )
    from qobuz_librarian.web import (
        flows,
        job_persistence,
    )
    monkeypatch.setattr(
        "qobuz_librarian.library.candidate_premise.validate_all",
        lambda _candidates: [],
    )
    monkeypatch.setattr(job_persistence, "_persist_locked", lambda _job: True)
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "t", "user_id": "u"})
    monkeypatch.setattr(webapp, "_TOKEN_VALID", True)
    monkeypatch.setattr(jm._scan_queue, "put", lambda item: None)
    qobuz_tracks = [
        {"title": "Silence", "media_number": 1, "isrc": "GB001"},
        {"title": "Hunter", "media_number": 1, "isrc": "GB002"},
    ]
    owned_tracks = [
        {"title": "Silence", "discnumber": 1, "isrc": "GB001"},
        {"title": "Hunter", "discnumber": 1, "isrc": "GB002"},
    ]
    monkeypatch.setattr(
        flows, "get_album",
        lambda album_id, _token: {
            "id": album_id,
            "title": "Third",
            "artist": {"name": "Portishead"},
            "tracks": {"items": qobuz_tracks},
        })
    monkeypatch.setattr(
        "qobuz_librarian.library.catalog.find_album_dir_filesystem",
        lambda alb: Path("/music/Portishead/Third")
        if alb.get("id") == "q123"
        else Path("/music/Portishead/Dummy")
        if alb.get("id") == "q456" else None)
    monkeypatch.setattr(
        "qobuz_librarian.library.catalog.find_existing_tracks",
        lambda _album, album_dir=None: (
            owned_tracks[:1] if album_dir.name == "Dummy" else owned_tracks,
            album_dir,
        ))

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
    finally:
        _remove_job(job)
        if split is not None:
            _remove_job(split)




def test_drop_owned_requires_every_expected_track(
        tmp_path, monkeypatch):
    """Empty and partial folders stay actionable; an exact complete one drops."""
    from qobuz_librarian.web import flows, job_persistence

    monkeypatch.setattr(job_persistence, "_persist_locked", lambda _job: True)

    shell = tmp_path / "Runnin' Wild (2019)"
    shell.mkdir()
    partial = tmp_path / "Partial Album (2010)"
    partial.mkdir()
    complete = tmp_path / "Complete Album (2010)"
    complete.mkdir()

    qobuz_tracks = [
        {"title": "Alpha", "media_number": 1, "isrc": "AA001"},
        {"title": "Beta", "media_number": 1, "isrc": "AA002"},
    ]
    alpha = {"title": "Alpha", "discnumber": 1, "isrc": "AA001"}
    beta = {"title": "Beta", "discnumber": 1, "isrc": "AA002"}
    monkeypatch.setattr(
        flows, "get_album",
        lambda album_id, _token: {
            "id": album_id,
            "title": album_id,
            "artist": {"name": "Airbourne"},
            "tracks": {"items": qobuz_tracks},
        })

    monkeypatch.setattr(
        "qobuz_librarian.library.catalog.find_album_dir_filesystem",
        lambda alb: shell if alb.get("id") == "empty1"
        else partial if alb.get("id") == "partial1"
        else complete if alb.get("id") == "complete1" else None)
    monkeypatch.setattr(
        "qobuz_librarian.library.catalog.find_existing_tracks",
        lambda _album, album_dir=None: (
            [] if album_dir == shell
            else [alpha] if album_dir == partial
            else [alpha, beta],
            album_dir,
        ))

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.add_candidate(kind="album", title="Runnin' Wild", artist="Airbourne",
                      payload={"album_id": "empty1"}, selected=True)
    job.add_candidate(kind="album", title="Partial Album", artist="Airbourne",
                      payload={"album_id": "partial1"}, selected=True)
    job.add_candidate(kind="album", title="Complete Album", artist="Airbourne",
                      payload={"album_id": "complete1"}, selected=True)
    try:
        dropped = flows.drop_owned_missing_candidates(job, "tok")
        titles = [c["title"] for c in job.candidates]
        assert "Runnin' Wild" in titles
        assert "Partial Album" in titles
        assert "Complete Album" not in titles
        assert dropped == 1
    finally:
        _remove_job(job)




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
        flags = {x["title"]: x["selected"] for x in job.candidates}
        assert flags == {"Third": False, "Ashes": True}
        # Empty query keeps the whole-tab behavior.
        client.post(f"/jobs/{job.id}/select-all",
                    data={"on": "1", "scope": "all", "tab": "missing", "q": ""})
        flags = {x["title"]: x["selected"] for x in job.candidates}
        assert flags == {"Third": True, "Ashes": True}
    finally:
        _remove_job(job)




def test_dismiss_rest_scoped_to_the_active_filter(client, monkeypatch):
    from qobuz_librarian.web import job_persistence
    monkeypatch.setattr(job_persistence, "_persist_locked", lambda _job: True)
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


def test_dismiss_rest_reports_partial_durable_progress(client, monkeypatch,
                                                       tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    monkeypatch.setattr(job_persistence, "_persist_locked", lambda _job: True)
    real_hide = hidden.hide
    hide_calls = 0

    def fail_second_hide(scope, items, gap_fill=None):
        nonlocal hide_calls
        hide_calls += 1
        if hide_calls == 2:
            raise OSError("inert hidden-store failure")
        return real_hide(scope, items, gap_fill=gap_fill)

    monkeypatch.setattr(hidden, "hide", fail_second_hide)
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.add_candidate(
        kind="album", title="First Durable Dismissal", artist="Alpha",
        payload={"year": "2001"}, selected=False,
    )
    job.add_candidate(
        kind="album", title="Still Actionable", artist="Beta",
        payload={"year": "2002"}, selected=False,
    )
    try:
        response = client.post(f"/jobs/{job.id}/dismiss-rest")
        assert response.status_code == 500
        assert response.json()["hidden"] == 1
        assert [c["title"] for c in job.candidates] == ["Still Actionable"]

        store = hidden.load()
        assert hidden.is_hidden(
            hidden.SCOPE_MISSING, "Alpha", "First Durable Dismissal", store,
        )
        assert not hidden.is_hidden(
            hidden.SCOPE_MISSING, "Beta", "Still Actionable", store,
        )
        rendered = client.get(f"/jobs/{job.id}")
        assert rendered.status_code == 200
        assert "Still Actionable" in rendered.text
        assert "First Durable Dismissal" not in rendered.text
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
        assert job.summary.startswith("All listed albums reviewed.")
        assert "result cap" in job.summary
        assert "3 artists couldn't be checked" in job.summary
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


def test_finished_sse_respects_zero_replay_tail(client):
    job = jm.Job(title="finished-job")
    job.status = jm.JobStatus.DONE
    job.log_lines = ["old line must not replay"]
    job.REPLAY_TAIL = 0
    jm.registry.add(job)
    try:
        with client.stream("GET", f"/api/jobs/{job.id}/stream") as response:
            body = "".join(response.iter_text())
        assert response.status_code == 200
        assert "event: done" in body
        assert "old line must not replay" not in body
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




def test_a_finished_job_keeps_its_log_across_a_restart(monkeypatch):
    """A finished job's log is the record of what a download actually did, so
    it has to outlive the process that wrote it.
    """
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    saved = jm.Job(title="Agalloch, The White EP", artist="Agalloch")
    saved.kind = "download"
    saved.push_line("  ✓  Download succeeded.")
    saved.push_line("  ✓  beets import succeeded.")
    saved.status = jm.JobStatus.DONE
    job_persistence.persist(saved)

    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    jm.restore_jobs({})

    restored = jm.registry.get(saved.id)
    assert restored is not None
    assert restored.log_lines == [
        "  ✓  Download succeeded.",
        "  ✓  beets import succeeded.",
    ]


def test_a_long_finished_log_is_stored_as_a_marked_tail(monkeypatch):
    """1000 finished rows are kept on disk, so a whole LOG_CAP-sized log each
    would put hundreds of MB in jobs.db. Keep the tail and say so, rather than
    letting the archive claim it is the whole log.
    """
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    saved = jm.Job(title="Library scan")
    saved.kind = "scan"
    for n in range(jm.Job.LOG_PERSIST_CAP + 40):
        saved.push_line(f"line {n}")
    saved.status = jm.JobStatus.DONE
    job_persistence.persist(saved)

    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    jm.restore_jobs({})

    restored = jm.registry.get(saved.id)
    assert len(restored.log_lines) == jm.Job.LOG_PERSIST_CAP
    assert restored.log_lines[0] == jm.Job._PERSIST_TRUNCATION_MARKER
    assert restored.log_lines[-1] == f"line {jm.Job.LOG_PERSIST_CAP + 39}"




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
    healthy = jm.Job(title="Healthy history")
    healthy.status = jm.JobStatus.DONE
    assert job_persistence.persist(broken)
    assert job_persistence.persist(healthy)

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

    jm.restore_jobs({"migration": migration_factory})

    restored_broken = jm.registry.get(broken.id)
    assert restored_broken.status == jm.JobStatus.FAILED
    assert "couldn't be restored" in restored_broken.error
    assert jm.registry.get(healthy.id).status == jm.JobStatus.DONE
    assert job_persistence.load_one(broken.id)["status"] == "failed"
    assert sent == [(broken.id, "failed")]


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


def test_library_review_rebuilds_from_saved_state_when_no_live_job(monkeypatch):
    """F1: with the baseline complete but no live library job (swept cancel,
    discarded scan job, corrupt restart row), the Missing Albums / Gap Fill
    review must rebuild from saved scan state, never 'Baseline ready' + no
    tabs. Retiring the review (discard / worked-through) blocks the rebuild."""
    from qobuz_librarian.library import library_scan_state
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_web_writes_paused", lambda: False)
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


def test_saved_library_review_is_not_published_without_durable_admission(monkeypatch):
    from qobuz_librarian.library import library_scan_state
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(webapp, "_web_writes_paused", lambda: False)
    original = library_scan_state.load()
    job = None
    try:
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
                            "detail": "2002",
                            "payload": {"album_id": "m1"},
                        }
                    ],
                },
            },
            complete=True,
        )
        monkeypatch.setattr(job_persistence, "admit", lambda _job: False)

        job = webapp._review_job_from_library_state()

        assert job is None
        assert not any(
            candidate.execute_kind == "library" and candidate.status == jm.JobStatus.AWAITING_REVIEW
            for candidate in jm.registry.all()
        )
    finally:
        library_scan_state._write_state(original)
        if job is not None:
            _remove_job(job)




def test_library_review_retirement_cannot_race_saved_state_reconstruction(monkeypatch):
    from qobuz_librarian.library import hidden, library_scan_state
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import job_persistence

    original = library_scan_state.load()
    old = None
    rebuilt = []
    entered_hidden_load = threading.Event()
    release_hidden_load = threading.Event()
    cancellation_finished = threading.Event()
    real_hidden_load = hidden.load
    try:
        monkeypatch.setattr(job_persistence, "persist", lambda _job: True)
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
                            "detail": "2002",
                            "payload": {"album_id": "m1"},
                        }
                    ],
                },
            },
            complete=True,
        )
        old = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
        old.execute_kind = "library"
        old.add_candidate(
            kind="album",
            title="The Mantle",
            artist="Agalloch",
            payload={"album_id": "m1"},
            selected=True,
        )

        def blocked_hidden_load():
            entered_hidden_load.set()
            assert release_hidden_load.wait(timeout=2)
            return real_hidden_load()

        monkeypatch.setattr(hidden, "load", blocked_hidden_load)
        builder = threading.Thread(
            target=lambda: rebuilt.append(webapp._review_job_from_library_state())
        )
        builder.start()
        assert entered_hidden_load.wait(timeout=2)

        def discard():
            try:
                assert jm.cancel_review(old) is True
            finally:
                cancellation_finished.set()

        canceler = threading.Thread(target=discard)
        canceler.start()
        cancellation_finished.wait(timeout=0.2)
        release_hidden_load.set()
        builder.join(timeout=2)
        canceler.join(timeout=2)

        assert not builder.is_alive()
        assert not canceler.is_alive()
        assert cancellation_finished.is_set()
        assert not any(
            candidate.execute_kind == "library" and candidate.status == jm.JobStatus.AWAITING_REVIEW
            for candidate in jm.registry.all()
        )
    finally:
        release_hidden_load.set()
        library_scan_state._write_state(original)
        if old is not None:
            _remove_job(old)
        for job in rebuilt:
            if job is not None and job is not old:
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
        # Candidates are present, so a rebuild WOULD produce a job, proving the
        # None is the retirement block holding, not an empty candidate list.
        assert webapp._review_job_from_library_state() is None
    finally:
        lss._write_state(original)






def test_cancel_mid_download_folds_every_unfinished_pick_back(monkeypatch):
    """Cancellation preserves failed, in-flight, and unstarted picks."""
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows, job_persistence

    _allow_legacy_candidate_execution(monkeypatch)
    monkeypatch.setattr(job_persistence, "_persist_locked", lambda _job: True)

    parked = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
    parked.execute_kind = "library"
    running = _inject_job(jm.JobStatus.RUNNING, "Library scan")
    running.execute_kind = "library"
    running.add_candidate(kind="album", title="Failed First", artist="Abigail",
                          payload={"album_id": "r1"}, selected=True)
    running.add_candidate(kind="album", title="In Flight", artist="Abigail",
                          payload={"album_id": "r2"}, selected=True)
    running.add_candidate(kind="album", title="Never Started", artist="Abigail",
                          payload={"album_id": "r3"}, selected=True)
    chosen = list(running.candidates)
    monkeypatch.setattr(flows.cfg, "ARTIST_API_DELAY", 0)
    monkeypatch.setattr(flows, "get_album", lambda aid, _t: {"id": aid})
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)

    def fake_process(full, *_a, **_k):
        if full["id"] == "r1":
            return {"result": "error", "imported": False, "n_ok": 0}
        running.cancel_requested = True
        return {"result": "cancelled"}

    monkeypatch.setattr(process_mod, "process_album", fake_process)
    try:
        flows.execute_albums(running, chosen, "tok")
        by_title = {c["title"]: c for c in parked.candidates}
        assert by_title.get("Failed First", {}).get("selected") is True
        assert by_title.get("In Flight", {}).get("selected") is True
        assert by_title.get("Never Started", {}).get("selected") is True
        assert running.summary == (
            "Stopped early. 0 albums downloaded, 1 album failed, "
            "1 album interrupted, 1 album not started. "
            "3 retry choices returned to Library, selected for retry."
        )
    finally:
        _remove_job(parked)
        _remove_job(running)


def test_missing_batch_allows_an_earlier_sibling_album_to_land(
        tmp_path, monkeypatch):
    """One artist's first download must not stale its next missing album."""
    from copy import deepcopy
    from types import SimpleNamespace

    from qobuz_librarian import config as cfg
    from qobuz_librarian.library.candidate_premise import capture
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows

    music = tmp_path / "music"
    artist_dir = music / "Artist"
    existing = artist_dir / "Existing"
    existing.mkdir(parents=True)
    (existing / "01.flac").write_bytes(b"existing audio")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "ARTIST_API_DELAY", 0)
    premise = capture("missing", artist_dir)
    assert premise is not None

    job = jm.Job(title="Library run", status=jm.JobStatus.RUNNING)
    job.execute_kind = "library"
    for album_id in ("First", "Second"):
        job.add_candidate(
            "album",
            album_id,
            "Artist",
            payload={
                "album_id": album_id,
                "_artist_dir_path": str(artist_dir),
                "_premise": deepcopy(premise),
            },
            selected=True,
        )
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
    assert "2/2 albums downloaded" in job.summary




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
        assert running.summary == "1/2 albums downloaded and imported."
        assert running.error == (
            "1 of 2 albums didn't finish. It is selected in Library for retry."
        )
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
    up when it is APPROVED, not reuse the value from the run that failed,
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
        # Approval must perform the live capability check and use its fresh token.
        from qobuz_librarian.api import client as api_client
        monkeypatch.setattr(
            api_client,
            "authorize_qobuz_action",
            lambda _access: SimpleNamespace(token="fresh-tok"),
        )
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






def test_new_release_run_recoveries_never_touch_the_library_review(monkeypatch):
    """A failed or cancelled NEW-RELEASE download run must not fold its albums
    into the parked Library review; new-release results never enter the
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
    fire; approve() restores the whole review instead, and folding here too
    would offer the same picks twice."""
    from qobuz_librarian.api.auth import AuthLost
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows, job_persistence

    _allow_legacy_candidate_execution(monkeypatch)

    monkeypatch.setattr(job_persistence, "_persist_locked", lambda _job: True)

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
    monkeypatch.setattr(
        process_mod,
        "process_album",
        lambda *_args, **_kwargs: {
            "imported": True,
            "n_ok": 1,
            "result": "downloaded",
            "dir": tmp_path,
        },
    )
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
        assert "1 album upgraded" in job.summary
        assert "2 albums selected for retry" in job.summary
    finally:
        if parked is not None:
            _remove_job(parked)


def test_upgrade_cancel_after_first_success_reparks_unfinished(
        monkeypatch, tmp_path):
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
    monkeypatch.setattr(
        flows,
        "get_album",
        lambda album_id, _token: {"id": album_id, "title": album_id},
    )
    job = jm.Job(title="Upgrade run", status=jm.JobStatus.RUNNING)
    job.execute_kind = "upgrade"
    job.execute_args = {
        "quality_signature": flows.upgrade_state.quality_signature(),
    }

    def process_album(album, *_args, **_kwargs):
        if album["id"] == "second":
            job.cancel_requested = True
            return {"result": "cancelled", "imported": False}
        return {
            "imported": True,
            "n_ok": 1,
            "result": "downloaded",
            "dir": tmp_path,
        }

    monkeypatch.setattr(process_mod, "process_album", process_album)
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
    finally:
        if parked is not None:
            _remove_job(parked)


def test_repair_auth_loss_after_first_success_reparks_unstarted(monkeypatch):
    from qobuz_librarian.api.auth import AuthLost
    from qobuz_librarian.web import flows, job_persistence

    _allow_legacy_candidate_execution(monkeypatch)
    monkeypatch.setattr(job_persistence, "_persist_locked", lambda _job: True)
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "build_args", object)
    monkeypatch.setattr(flows, "_note_staging_wait", lambda *_a, **_k: None)
    monkeypatch.setattr(flows.time, "sleep", lambda _seconds: None)

    def redownload(payload, _token, **_kwargs):
        if payload["album_id"] == "second":
            raise AuthLost("expired")
        return {"imported": True, "n_ok": 1, "n_fail": 0}

    monkeypatch.setattr(flows, "_redownload_damaged_album", redownload)
    monkeypatch.setattr(flows, "_refresh_after_local_album_change",
                        lambda *_args, **_kwargs: None)
    job = jm.Job(title="Repair run", status=jm.JobStatus.RUNNING)
    job.execute_kind = "repair"
    for title in ("first", "second", "third"):
        job.add_candidate(
            "redownload",
            title,
            "Artist",
            payload={
                "album_id": title,
                "album_dir": f"/music/Artist/{title}",
                "artist_name": "Artist",
            },
            selected=True,
        )
    parked = None
    try:
        with pytest.raises(AuthLost):
            flows.execute_repairs(job, list(job.candidates), "token")

        parked = next(
            candidate
            for candidate in jm.registry.awaiting_review()
            if candidate.execute_kind == "repair"
        )
        assert {
            candidate["title"]: candidate["selected"]
            for candidate in parked.candidates
        } == {"second": True, "third": True}
        assert "1 album repaired" in job.summary
        assert "2 albums selected for retry" in job.summary
    finally:
        if parked is not None:
            _remove_job(parked)


def test_repair_cancel_after_first_success_reparks_unfinished(monkeypatch):
    from qobuz_librarian.web import flows, job_persistence

    _allow_legacy_candidate_execution(monkeypatch)
    monkeypatch.setattr(job_persistence, "_persist_locked", lambda _job: True)
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "build_args", object)
    monkeypatch.setattr(flows, "_note_staging_wait", lambda *_a, **_k: None)
    monkeypatch.setattr(flows.time, "sleep", lambda _seconds: None)
    job = jm.Job(title="Repair run", status=jm.JobStatus.RUNNING)
    job.execute_kind = "repair"

    def redownload(payload, _token, **_kwargs):
        if payload["album_id"] == "second":
            job.cancel_requested = True
            return {"result": "cancelled", "cancelled": True}
        return {"imported": True, "n_ok": 1, "n_fail": 0}

    monkeypatch.setattr(flows, "_redownload_damaged_album", redownload)
    monkeypatch.setattr(flows, "_refresh_after_local_album_change",
                        lambda *_args, **_kwargs: None)
    for title in ("first", "second", "third"):
        job.add_candidate(
            "redownload",
            title,
            "Artist",
            payload={
                "album_id": title,
                "album_dir": f"/music/Artist/{title}",
                "artist_name": "Artist",
            },
            selected=True,
        )
    parked = None
    try:
        flows.execute_repairs(job, list(job.candidates), "token")

        parked = next(
            candidate
            for candidate in jm.registry.awaiting_review()
            if candidate.execute_kind == "repair"
        )
        assert {
            candidate["title"]: candidate["selected"]
            for candidate in parked.candidates
        } == {"second": True, "third": True}
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
        queue_before = client.get("/queue")
        job_before = client.get(f"/jobs/{recovery.id}")
        assert f'action="/jobs/{recovery.id}/cancel"' not in queue_before.text
        assert f'action="/jobs/{recovery.id}/cancel"' not in job_before.text

        individual = client.post(f"/jobs/{recovery.id}/cancel", follow_redirects=False)
        assert individual.status_code == 303
        refused = client.get(individual.headers["location"])
        assert "cannot be canceled until its saved step settles" in refused.text
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












def test_clear_history_keeps_memory_and_reports_failed_durable_delete(client, monkeypatch):
    from qobuz_librarian.web import job_persistence

    finished = _inject_job(jm.JobStatus.DONE, "History that must survive")
    monkeypatch.setattr(
        job_persistence,
        "clear_history",
        lambda **_kwargs: False,
    )
    try:
        response = client.post("/queue/clear", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"].startswith("/queue/history?error=")
        assert jm.registry.get(finished.id) is finished
    finally:
        _remove_job(finished)




def test_download_partial_album_proceeds_to_gap_fill(client, monkeypatch):
    from pathlib import Path

    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as cat_mod
    import qobuz_librarian.modes.process as proc_mod
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    album = {"id": "gap1", "title": "Gappy", "version": "Expanded Edition",
             "artist": {"name": "A"},
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
    assert "Gappy (Expanded Edition)" in r.text
    new_jobs = [j for j in list(jm.registry._jobs.values())
                if getattr(j, "album_id", None) == "gap1"]
    assert len(new_jobs) == 1
    job = new_jobs[0]
    assert job.edition == "Expanded Edition"
    try:
        _wait_for(lambda: job.status in (jm.JobStatus.DONE, jm.JobStatus.FAILED))
        assert "Gappy (Expanded Edition)" in client.get(f"/jobs/{job.id}").text
        status = client.get(f"/api/jobs/{job.id}/status").json()
        assert status["edition"] == "Expanded Edition"
        assert status["display_title"] == "Gappy (Expanded Edition)"
        assert "Gappy (Expanded Edition)" in client.get("/queue/history").text
    finally:
        _remove_job(job)


def test_incomplete_new_album_retries_broken_tracks_not_lossy_ones(
        client, monkeypatch):
    import contextlib
    from types import SimpleNamespace

    import qobuz_librarian.library.catalog as catalog_mod
    import qobuz_librarian.library.hidden as hidden_mod
    import qobuz_librarian.modes.process as process_mod
    import qobuz_librarian.web.app as app_mod
    import qobuz_librarian.web.flows as flows_mod
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()
    album = {
        "id": "partial-release",
        "title": "Third",
        "artist": {"name": "Portishead"},
        "tracks": {
            "items": [{"id": "one"}, {"id": "two"}, {"id": "three"}]
        },
    }
    folded = []
    results = iter([
        {
            "result": "partial",
            "imported": True,
            "n_ok": 2,
            "n_fail": 0,
            "n_lossy": 1,
            "n_broken": 1,
            "n_lossy_only": 0,
            "broken_tracks": ["two"],
            "siblings_preserved": ["/music/Portishead/Third (Alt)"],
        },
        {
            "result": "downloaded",
            "imported": True,
            "n_ok": 1,
            "n_fail": 0,
            "n_lossy": 0,
        },
        {
            "result": "partial",
            "imported": True,
            "n_ok": 2,
            "n_fail": 0,
            "n_lossy": 1,
            "n_broken": 0,
            "n_lossy_only": 1,
            "lossy_tracks": ["three"],
        },
    ])
    monkeypatch.setattr(catalog_mod, "is_lossless_album", lambda _album: False)
    monkeypatch.setattr(
        process_mod,
        "process_album",
        lambda *_args, **_kwargs: next(results),
    )
    monkeypatch.setattr(flows_mod, "build_args", lambda: SimpleNamespace())
    monkeypatch.setattr(flows_mod, "_note_staging_wait", lambda *_a, **_k: None)
    monkeypatch.setattr(
        flows_mod, "_refresh_after_local_album_change", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        flows_mod, "prune_library_review_candidates", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        flows_mod,
        "_fold_partial_gap_fill",
        lambda *_args: folded.append(_args),
    )
    monkeypatch.setattr(hidden_mod, "unmark_single", lambda *_a, **_k: None)
    monkeypatch.setattr(jm, "staging_lock", contextlib.nullcontext)
    monkeypatch.setattr(
        app_mod,
        "_read_creds",
        lambda: {"auth_token": "token", "user_id": "user"},
    )
    monkeypatch.setattr(app_mod, "_TOKEN_VALID", True)
    monkeypatch.setattr(app_mod, "_get_token", lambda: "token")
    monkeypatch.setattr(
        "qobuz_librarian.api.search.get_album", lambda _album_id, _token: album
    )

    job = jm.Job(
        title=album["title"],
        artist=album["artist"]["name"],
        album_id=album["id"],
        status=jm.JobStatus.RUNNING,
    )
    jm.registry.add(job)
    assert job_persistence.persist(job)
    try:
        jm._run_task(job, app_mod._make_download_run(album, "token"))

        saved = job_persistence.load_one(job.id)
        assert saved["status"] == "failed"
        assert saved["summary"] == "2 tracks downloaded."
        assert saved["error"] == "1 track is still missing. Retry fetches it."
        assert saved["attention"] == "partial"
        assert folded and folded[0][2] == 1

        history = client.get("/queue/history").text
        assert (
            'title="This album is missing tracks; open it for details."'
            ">Incomplete</span>"
        ) in history
        job_page = client.get(f"/jobs/{job.id}").text
        for page in (job_page, history):
            assert ">Retry</button>" in page
        assert job_persistence.load_one(job.id)["attention"] == ""

        jm.start_worker()
        response = client.post(f"/jobs/{job.id}/retry")
        assert response.status_code == 200
        assert _wait_for(
            lambda: any(
                item.id != job.id and item.album_id == album["id"]
                for item in jm.registry.all()
            )
        )
        retry = next(
            item for item in jm.registry.all()
            if item.id != job.id and item.album_id == album["id"]
        )
        assert _wait_for(lambda: retry.status == jm.JobStatus.DONE)
        assert retry.summary == "1 track downloaded."

        unavailable = jm.Job(
            title=album["title"],
            artist=album["artist"]["name"],
            album_id=album["id"],
            status=jm.JobStatus.RUNNING,
        )
        jm.registry.add(unavailable)
        assert job_persistence.persist(unavailable)
        jm._run_task(
            unavailable,
            app_mod._make_download_run(album, "token"),
        )

        saved = job_persistence.load_one(unavailable.id)
        assert saved["status"] == "failed"
        assert saved["summary"] == "2 tracks downloaded."
        assert saved["error"] == (
            "1 track is only available lossy on Qobuz. The album is "
            "incomplete and needs another source."
        )
        assert saved["attention"] == "lossy"
        assert saved["execute_args"]["retry_disabled"] == "lossy"

        history = client.get("/queue/history").text
        assert "Lossless unavailable" in history
        assert f'action="/jobs/{unavailable.id}/retry"' not in history
        job_page = client.get(f"/jobs/{unavailable.id}").text
        assert ">Retry</button>" not in job_page
        before = {item.id for item in jm.registry.all()}
        response = client.post(
            f"/jobs/{unavailable.id}/retry",
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert {item.id for item in jm.registry.all()} == before
    finally:
        if "unavailable" in locals():
            _remove_job(unavailable)
        if "retry" in locals():
            _remove_job(retry)
        _remove_job(job)


def test_settings_save_rejects_out_of_enum_quality(tmp_path, monkeypatch):
    import json

    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(ss, "_any_active_job", lambda: False)
    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 4)
    monkeypatch.setattr(cfg, "LYRICS_ENABLED", True)

    assert ss.save({"STREAMRIP_QUALITY": "99"})[0] is None
    assert not (tmp_path / "s.json").exists()
    # A mixed forged submission is rejected as one unit: the valid sibling
    # must not apply while the invalid enum is silently dropped.
    assert (
        ss.save(
            {
                "STREAMRIP_QUALITY": "99",
                "LYRICS_ENABLED": False,
            }
        )[0]
        is None
    )
    assert cfg.STREAMRIP_QUALITY == 4
    assert cfg.LYRICS_ENABLED is True
    assert not (tmp_path / "s.json").exists()
    # A valid value still persists.
    assert ss.save({"STREAMRIP_QUALITY": "2"})[0] is True
    assert json.loads((tmp_path / "s.json").read_text())["STREAMRIP_QUALITY"] == "2"


def test_downsample_keep_choice_can_go_back_to_unchosen(tmp_path, monkeypatch):
    """Unset is a real answer for this one: the choice starts unset, the first
    downsample asks for it, and picking the unset entry again is the only way
    to be asked a second time."""
    import json

    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(ss, "_any_active_job", lambda: False)
    monkeypatch.setattr(ss, "_pending_apply", None)
    monkeypatch.setitem(ss._ENV_DEFAULTS, "DOWNSAMPLE_KEEP_ORIGINALS", None)
    monkeypatch.setattr(cfg, "DOWNSAMPLE_KEEP_ORIGINALS", None)

    assert ss.save({"DOWNSAMPLE_KEEP_ORIGINALS": "keep"})[0] is True
    assert cfg.DOWNSAMPLE_KEEP_ORIGINALS == "keep"

    assert ss.save({"DOWNSAMPLE_KEEP_ORIGINALS": ""})[0] is True
    assert cfg.DOWNSAMPLE_KEEP_ORIGINALS is None
    saved = json.loads((tmp_path / "s.json").read_text())
    assert "DOWNSAMPLE_KEEP_ORIGINALS" not in saved


def test_environment_qobuz_credentials_cannot_be_shadowed_by_the_form(client, monkeypatch):
    """The form must not claim to replace an environment-owned credential."""
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian import config as cfg

    monkeypatch.setattr(cfg, "QOBUZ_USER_AUTH_TOKEN", "environment-token")
    monkeypatch.setattr(cfg, "QOBUZ_USER_ID", "environment-user")
    writes = []
    monkeypatch.setattr(
        app_mod,
        "_write_creds",
        lambda user_id, token: writes.append((user_id, token)) or True,
    )
    monkeypatch.setattr(app_mod, "_classify_token", lambda _token: "ok")

    response = client.post(
        "/settings",
        data={"user_id": "form-user", "auth_token": "form-secret-token"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "form-secret-token" not in response.text
    assert writes == []


def test_settings_accepts_a_token_without_downloader_identity(client, monkeypatch):
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian.api.auth import AuthOutcome

    writes = []
    monkeypatch.setattr(app_mod, "_read_creds", lambda: {})
    monkeypatch.setattr(
        app_mod,
        "_write_creds",
        lambda user_id, token: writes.append((user_id, token)) or True,
    )
    monkeypatch.setattr(
        app_mod,
        "_classify_token",
        lambda _token: AuthOutcome.ACCEPTED,
    )
    monkeypatch.setattr(
        app_mod,
        "_credentials_snapshot",
        lambda: app_mod.credentials_from_values("", "token-only"),
    )

    response = client.post(
        "/settings",
        data={"user_id": "", "auth_token": "token-only"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?connected=1"
    assert writes == [("", "token-only")]


def test_settings_rejected_candidate_token_cannot_fire_auth_loss_hook(
        client, monkeypatch):
    import qobuz_librarian.api.client as client_mod
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian.api.auth import AuthOutcome, credentials_from_values

    active = credentials_from_values(
        "saved-user", "saved-token", source="streamrip"
    )
    probes = []
    hooks = []
    writes = []

    def reject_candidate(token, *, report_auth=True):
        probes.append((token, report_auth))
        return AuthOutcome.REJECTED

    monkeypatch.setattr(
        app_mod,
        "_read_creds",
        lambda: {
            "user_id": active.user_id,
            "auth_token": active.token,
            "_source": active.source,
        },
    )
    monkeypatch.setattr(app_mod, "_qobuz_token_is_env_owned", lambda: False)
    monkeypatch.setattr(client_mod, "probe_qobuz", reject_candidate)
    monkeypatch.setattr(
        app_mod.job_mgr,
        "fire_auth_lost_hook",
        lambda: hooks.append(True),
    )
    monkeypatch.setattr(
        app_mod,
        "_write_creds",
        lambda user_id, token: writes.append((user_id, token)) or True,
    )
    monkeypatch.setattr(app_mod, "_TOKEN_VALID", True)
    monkeypatch.setattr(app_mod, "_TOKEN_GENERATION", active.generation)

    response = client.post(
        "/settings",
        data={"user_id": "candidate-user", "auth_token": "candidate-token"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert probes == [("candidate-token", False)]
    assert hooks == []
    assert writes == []
    assert app_mod._TOKEN_VALID is True
    assert app_mod._TOKEN_GENERATION == active.generation


def test_settings_refuses_account_change_during_remote_file_work(
        client, monkeypatch):
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian.api.auth import AuthOutcome, credentials_from_values

    running = jm.Job(
        title="Album",
        status=jm.JobStatus.RUNNING,
    )
    jm.registry.add(running)
    active = credentials_from_values("old-user", "old-token", source="streamrip")
    writes = []
    monkeypatch.setattr(
        app_mod,
        "_read_creds",
        lambda: {
            "user_id": active.user_id,
            "auth_token": active.token,
            "_source": active.source,
        },
    )
    monkeypatch.setattr(app_mod, "_classify_token", lambda _token: AuthOutcome.ACCEPTED)
    monkeypatch.setattr(
        app_mod,
        "_write_creds",
        lambda user_id, token: writes.append((user_id, token)) or True,
    )
    try:
        response = client.post(
            "/settings",
            data={"user_id": "new-user", "auth_token": "new-token"},
            follow_redirects=False,
        )

        assert response.status_code == 200
        assert "already running" in response.text
        assert writes == []
    finally:
        _remove_job(running)






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

    def restore_jobs(factories, *, durable_recovery_clear,
                     durable_recovery_job_id):
        assert factories is app_mod._RESUME_EXECUTE
        assert durable_recovery_clear is True
        assert durable_recovery_job_id is None
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
        assert web_auth.set_credentials("admin", "hunter2hunter2!")
    return _SameThreadASGIClient(app_mod.app)


def test_login_page_says_so_while_locked_out(monkeypatch, tmp_path):
    """The lockout was invisible on the form: it looked normal until you filled
    it in and submitted again."""
    from qobuz_librarian.web import auth as web_auth

    # The failure counters are module state; give this test its own so the
    # lockout it deliberately triggers doesn't follow the rest of the suite.
    monkeypatch.setattr(web_auth, "_login_failures", {})
    monkeypatch.setattr(web_auth, "_user_failures", {})

    with _enable_auth(monkeypatch, tmp_path) as c:
        for _ in range(6):
            c.get("/login")
            tok = c.cookies.get("ql_csrf")
            last = c.post("/login",
                          data={"username": "admin", "password": "nope",
                                "_csrf_token": tok},
                          headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert last.status_code == 401

        assert "ql-notice-error" in c.get("/login").text


def test_an_untrusted_proxy_address_is_called_out(monkeypatch, caplog):
    """Left unnamed in FORWARDED_ALLOW_IPS, a proxy makes every visitor arrive
    as one address and the failed-login limit silently covers the whole
    deployment, so a stranger's wrong guesses lock the owner out."""
    from qobuz_librarian.web import auth as web_auth

    def req(peer, forwarded):
        return SimpleNamespace(client=SimpleNamespace(host=peer),
                               headers={"x-forwarded-for": forwarded})

    monkeypatch.setattr(web_auth, "_warned_untrusted_proxy", False)
    with caplog.at_level("WARNING"):
        assert web_auth.client_ip(req("172.30.0.1", "203.0.113.7")) == "172.30.0.1"
    assert "FORWARDED_ALLOW_IPS=172.30.0.1" in caplog.text

    # Resolved by uvicorn: the address is one of the forwarded entries.
    monkeypatch.setattr(web_auth, "_warned_untrusted_proxy", False)
    caplog.clear()
    with caplog.at_level("WARNING"):
        assert web_auth.client_ip(req("203.0.113.7", "203.0.113.7")) == "203.0.113.7"
    assert "FORWARDED_ALLOW_IPS" not in caplog.text


def test_a_correct_password_still_works_while_locked_out(monkeypatch, tmp_path):
    """The throttle used to refuse before checking the password, so behind a
    proxy a stranger's wrong guesses locked the owner out of their own library
    with a container restart as the only way back in."""
    from qobuz_librarian.web import auth as web_auth

    monkeypatch.setattr(web_auth, "_login_failures", {})
    monkeypatch.setattr(web_auth, "_user_failures", {})

    with _enable_auth(monkeypatch, tmp_path) as c:
        for _ in range(6):
            c.get("/login")
            tok = c.cookies.get("ql_csrf")
            c.post("/login", data={"username": "admin", "password": "nope",
                                   "_csrf_token": tok},
                   headers={"X-CSRF-Token": tok}, follow_redirects=False)

        c.get("/login")
        tok = c.cookies.get("ql_csrf")
        r = c.post("/login",
                   data={"username": "admin", "password": "hunter2hunter2!",
                         "_csrf_token": tok},
                   headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert r.status_code == 303
        assert c.get("/", follow_redirects=False).status_code == 200


def test_signed_out_error_page_keeps_the_app_shell_hidden(monkeypatch, tmp_path):
    """A tokenless POST is refused before the auth gate runs, so its error page
    was handing a signed-out visitor the whole nav and a Log out button with no
    route back to the sign-in form."""
    with _enable_auth(monkeypatch, tmp_path) as client:
        client.cookies.clear()
        response = client.post("/login", data={"username": "a", "password": "b"},
                               follow_redirects=False)
        assert response.status_code == 403
        assert "ql-sidebar-nav" not in response.text
        assert "/login" in response.text

        # A missing static file is outside the gate too, and reached the same
        # renderer.
        response = client.get("/static/does-not-exist.css", follow_redirects=False)
        assert response.status_code == 404
        assert "ql-sidebar-nav" not in response.text


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


def test_new_password_policy_covers_setup_and_environment(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import auth as web_auth

    with _enable_auth(monkeypatch, tmp_path, configure=False) as client:
        client.get("/setup")
        token = client.cookies.get("ql_csrf")

        short = client.post(
            "/setup",
            data={"username": "admin", "password": "x" * 14,
                  "confirm": "x" * 14, "_csrf_token": token},
            headers={"X-CSRF-Token": token}, follow_redirects=False,
        )
        assert short.status_code == 400
        assert not cfg.WEB_AUTH_FILE.exists()

        # Spaces used to satisfy the length rule on their own, so a lean on the
        # space bar could set the admin password.
        spaces = client.post(
            "/setup",
            data={"username": "admin", "password": " " * 20,
                  "confirm": " " * 20, "_csrf_token": token},
            headers={"X-CSRF-Token": token}, follow_redirects=False,
        )
        assert spaces.status_code == 400
        assert not cfg.WEB_AUTH_FILE.exists()

        common = client.post(
            "/setup",
            data={"username": "admin", "password": "QOBUZ LIBRARIAN",
                  "confirm": "QOBUZ LIBRARIAN", "_csrf_token": token},
            headers={"X-CSRF-Token": token}, follow_redirects=False,
        )
        assert common.status_code == 400
        assert "QOBUZ LIBRARIAN" not in common.text
        assert not cfg.WEB_AUTH_FILE.exists()

        passphrase = "Café orbit moon"
        created = client.post(
            "/setup",
            data={"username": "admin", "password": passphrase,
                  "confirm": passphrase, "_csrf_token": token},
            headers={"X-CSRF-Token": token}, follow_redirects=False,
        )
        assert created.status_code == 303
        assert web_auth.verify_login("admin", passphrase)

    seeded_auth = tmp_path / "seeded_auth.json"
    monkeypatch.setattr(cfg, "WEB_AUTH_FILE", seeded_auth)
    monkeypatch.setenv("WEB_AUTH_USER", "admin")
    monkeypatch.setenv("WEB_AUTH_PASSWORD", "x" * 14)
    monkeypatch.delenv("WEB_AUTH_PASSWORD_FILE", raising=False)
    with pytest.raises(web_auth.PasswordRejected,
                       match="at least 15 characters"):
        web_auth.apply_env_credentials()
    assert not seeded_auth.exists()

    password_file = tmp_path / "web_password"
    password_file.write_text("ember orbit atlas\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "WEB_AUTH_FILE", tmp_path / "file_auth.json")
    monkeypatch.delenv("WEB_AUTH_PASSWORD")
    monkeypatch.setenv("WEB_AUTH_PASSWORD_FILE", str(password_file))
    assert web_auth.apply_env_credentials() == "applied"
    assert web_auth.verify_login("admin", "ember orbit atlas")

    monkeypatch.setattr(cfg, "WEB_AUTH_FILE", tmp_path / "missing_file_auth.json")
    monkeypatch.setenv(
        "WEB_AUTH_PASSWORD_FILE",
        str(tmp_path / "missing_web_password"),
    )
    with pytest.raises(web_auth.CredentialSeedError,
                       match="WEB_AUTH_PASSWORD_FILE could not be read"):
        web_auth.apply_env_credentials()
    assert not cfg.WEB_AUTH_FILE.exists()


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


@pytest.mark.parametrize(
    ("seed_status", "message"),
    [
        ("partial", "Incomplete web login seed"),
        ("failed", "seeded web login could not be saved"),
    ],
)
def test_startup_refuses_an_explicit_web_login_seed_that_left_setup_open(
        monkeypatch, seed_status, message):
    from qobuz_librarian.ui_cli import logging as cli_logging
    from qobuz_librarian.web import app as app_mod
    from qobuz_librarian.web import auth as web_auth

    monkeypatch.setattr(cli_logging, "attach_file_handler", lambda *_args: None)
    monkeypatch.setattr(web_auth, "auth_disabled", lambda: False)
    monkeypatch.setattr(web_auth, "apply_env_credentials", lambda: seed_status)
    monkeypatch.setattr(web_auth, "credentials_configured", lambda: False)

    async def start():
        async with app_mod._lifespan(app_mod.app):
            raise AssertionError("startup should have refused the open setup")

    with pytest.raises(RuntimeError, match=message):
        asyncio.run(start())


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
                   data={"username": "admin", "password": "hunter2hunter2!",
                         "_csrf_token": tok},
                   headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        # The session cookie now opens a protected route.
        assert c.get("/", follow_redirects=False).status_code == 200


def test_login_refuses_an_unsaved_session(monkeypatch, tmp_path, caplog):
    from qobuz_librarian.web import auth as web_auth

    monkeypatch.setattr(web_auth, "_SESSIONS_FILE", tmp_path / "sessions.json")
    real_replace = web_auth.os.replace
    real_digest = web_auth._token_digest
    issued_tokens = []

    def fail_session_replace(source, destination):
        if Path(destination) == web_auth._SESSIONS_FILE:
            raise OSError("session volume unavailable")
        return real_replace(source, destination)

    def capture_token(token):
        issued_tokens.append(token)
        return real_digest(token)

    with _enable_auth(monkeypatch, tmp_path) as client:
        client.get("/login")
        token = client.cookies.get("ql_csrf")
        monkeypatch.setattr(web_auth.os, "replace", fail_session_replace)
        monkeypatch.setattr(web_auth, "_token_digest", capture_token)

        failed = client.post(
            "/login",
            data={"username": "admin", "password": "hunter2hunter2!",
                  "_csrf_token": token},
            headers={"X-CSRF-Token": token}, follow_redirects=False,
        )
        assert failed.status_code == 503
        assert "ql_session" not in failed.cookies
        assert issued_tokens[-1] not in failed.text
        assert issued_tokens[-1] not in caplog.text
        assert not list(tmp_path.glob(".qobuz_web_sessions.*.tmp"))
        assert client.get("/", follow_redirects=False).status_code == 303

        monkeypatch.setattr(web_auth.os, "replace", real_replace)
        recovered = client.post(
            "/login",
            data={"username": "admin", "password": "hunter2hunter2!",
                  "_csrf_token": token},
            headers={"X-CSRF-Token": token}, follow_redirects=False,
        )
        assert recovered.status_code == 303
        assert recovered.cookies.get("ql_session")
        assert client.get("/", follow_redirects=False).status_code == 200


def test_authenticated_pages_are_not_stored_in_the_browser_cache(
        monkeypatch, tmp_path):
    with _enable_auth(monkeypatch, tmp_path) as c:
        c.get("/login")
        tok = c.cookies.get("ql_csrf")
        c.post(
            "/login",
            data={"username": "admin", "password": "hunter2hunter2!",
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
    from qobuz_librarian.web import auth as web_auth

    with _enable_auth(monkeypatch, tmp_path) as c:
        r = c.get("/queue", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login?next=/queue"
        r = c.get("/login?next=/queue")
        assert 'name="next" value="/queue"' in r.text
        tok = c.cookies.get("ql_csrf")
        r = c.post("/login",
                   data={"username": "admin", "password": "hunter2hunter2!",
                         "_csrf_token": tok, "next": "/queue"},
                   headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/queue"

        web_auth.revoke_all_sessions()
        expired = c.get(
            "/api/diagnostics",
            headers={"HX-Request": "true",
                     "HX-Current-URL": "http://testserver/library?tab=gaps"},
            follow_redirects=False,
        )
        assert expired.status_code == 401
        assert expired.headers["HX-Redirect"] == "/login?next=/library%3Ftab%3Dgaps"

        login = c.get(expired.headers["HX-Redirect"])
        assert 'name="next" value="/library?tab=gaps"' in login.text
        tok = c.cookies.get("ql_csrf")
        returned = c.post(
            "/login",
            data={"username": "admin", "password": "hunter2hunter2!",
                  "_csrf_token": tok, "next": "/library?tab=gaps"},
            headers={"X-CSRF-Token": tok}, follow_redirects=False,
        )
        assert returned.headers["location"] == "/library?tab=gaps"

        web_auth.revoke_all_sessions()
        plain_api = c.get("/api/diagnostics", follow_redirects=False)
        assert plain_api.status_code == 401
        assert plain_api.json() == {"detail": "authentication required"}
        assert "HX-Redirect" not in plain_api.headers

        for current_url in (
            "",
            "https://elsewhere.example/queue",
            "not a URL",
            "http://testserver/" + "x" * 513,
        ):
            headers = {"HX-Request": "true"}
            if current_url:
                headers["HX-Current-URL"] = current_url
            refused = c.get(
                "/api/diagnostics", headers=headers, follow_redirects=False)
            assert refused.headers["HX-Redirect"] == "/login"


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
                   data={"username": "admin", "password": "hunter2hunter2!",
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
    assert "isn't there anymore" in r.text


def test_missing_repair_recovery_can_be_acknowledged_and_cleared(client, tmp_path, monkeypatch):
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()
    backup = tmp_path / "repair-backups" / "album-backup"
    backup.mkdir(parents=True)
    job = jm.Job(title="Repair needing recovery")
    job.execute_kind = "repair"
    job.status = jm.JobStatus.FAILED
    job.finished_at = time.time()
    job.attention = "recovery"
    job.summary = "Checked 4 albums; 1 was repaired."
    job.error = "1 album couldn't be repaired."
    job.recoveries = [_repair_recovery_record(backup)]
    job.LOG_CAP = 1
    job.log_lines = [
        f"prior line {number}" for number in range(job._LOG_SLACK + 1)
    ]
    jm.registry.add(job)
    try:
        assert job_persistence.persist(job)
        history = client.get("/queue/history").text
        assert job.summary in history

        client.post(f"/jobs/{job.id}/acknowledge-recovery")
        assert job.recoveries

        backup.rmdir()
        backup.parent.rmdir()
        client.post(f"/jobs/{job.id}/acknowledge-recovery")
        assert job.recoveries

        backup.parent.mkdir()
        client.post(f"/jobs/{job.id}/acknowledge-recovery")

        saved = job_persistence.load_one(job.id)
        assert saved["recoveries"] == []
        assert saved["attention"] == ""
        assert str(backup) in saved["log_lines"][-1]
        assert str(backup) in job.log_lines[-1]
        assert job_persistence.recovery_history() == []

        job_persistence.clear_history()
        assert job_persistence.load_one(job.id) is None
    finally:
        _remove_job(job)


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
    finally:
        _remove_job(job)


def test_restore_downsample_backup_marks_upgrade_stale(
        client, tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import backup as backup_mod
    from qobuz_librarian.library import generation_state
    from qobuz_librarian.quality import decision
    from qobuz_librarian.web import flows, job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path / "music")
    origin = tmp_path / "music" / "Artist" / "Album (2020)"
    origin.mkdir(parents=True)
    source = origin / "01 - Song.flac"
    source.write_bytes(b"hi-res original")
    carried, copied = backup_mod.stash_downsample_originals([source], origin)
    assert carried is not None and carried.complete is True
    assert copied == {source}
    source.write_bytes(b"downsampled copy")

    refreshed = []
    marked = []
    monkeypatch.setattr(
        decision,
        "clear_local_album_cap",
        lambda album_dir: refreshed.append(("cap", album_dir)),
    )
    monkeypatch.setattr(
        flows,
        "_refresh_downsample_artist_state",
        lambda artist_dir: refreshed.append(("downsample", artist_dir)),
    )
    monkeypatch.setattr(
        generation_state,
        "output_is_current",
        lambda surface: surface == "upgrade",
    )
    monkeypatch.setattr(
        generation_state,
        "mark_output_status",
        lambda surface, status, **kwargs: (
            marked.append((surface, status, kwargs.get("reason"))) or True
        ),
    )

    r = client.post("/backups/restore", data={"backup": carried.name})

    assert r.status_code == 200
    assert source.read_bytes() == b"hi-res original"
    assert not carried.exists()
    assert refreshed == [
        ("cap", origin),
        ("downsample", origin.parent),
    ]
    assert marked == [
        (
            "upgrade",
            "stale",
            "Upgrade needs refresh after Downsample was undone.",
        )
    ]


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


def test_auth_loss_hook_is_generation_bound_and_idempotent(monkeypatch):
    from qobuz_librarian.api.auth import (
        AuthEvidence,
        AuthOutcome,
        credentials_from_values,
    )
    from qobuz_librarian.web import app as app_mod

    active = [credentials_from_values("user", "old-token", source="web")]
    calls = []
    monkeypatch.setattr(
        app_mod,
        "_read_creds",
        lambda: {
            "auth_token": active[0].token,
            "user_id": active[0].user_id,
            "_source": active[0].source,
        },
    )
    monkeypatch.setattr(app_mod.job_mgr, "fire_auth_lost_hook",
                        lambda: calls.append(1))
    monkeypatch.setattr(app_mod, "_TOKEN_VALID", None)
    monkeypatch.setattr(app_mod, "_TOKEN_GENERATION", None)
    monkeypatch.setattr(app_mod, "_AUTH_LOSS_NOTIFIED_GENERATIONS", set())

    rejected = AuthEvidence(active[0].generation, AuthOutcome.REJECTED)
    app_mod._on_auth_state(rejected)
    app_mod._on_auth_state(rejected)
    app_mod._on_auth_state(
        AuthEvidence(active[0].generation, AuthOutcome.ACCEPTED)
    )
    app_mod._on_auth_state(rejected)
    assert calls == [1]

    old_generation = active[0].generation
    active[0] = credentials_from_values("user", "new-token", source="web")
    app_mod._on_auth_state(
        AuthEvidence(old_generation, AuthOutcome.ACCEPTED)
    )
    assert app_mod._token_valid_for(active[0]) is None
    app_mod._on_auth_state(
        AuthEvidence(active[0].generation, AuthOutcome.REJECTED)
    )
    assert calls == [1, 1]


def test_startup_probe_rejection_fires_the_auth_loss_hook_once(monkeypatch):
    from qobuz_librarian.api.auth import AuthOutcome, credentials_from_values
    from qobuz_librarian.web import app as app_mod

    credentials = credentials_from_values(
        "user", "rejected-token", source="web"
    )
    calls = []
    monkeypatch.setattr(
        app_mod,
        "_read_creds",
        lambda: {
            "auth_token": credentials.token,
            "user_id": credentials.user_id,
            "_source": credentials.source,
        },
    )
    monkeypatch.setattr(
        app_mod, "_classify_token", lambda _token: AuthOutcome.REJECTED
    )
    monkeypatch.setattr(
        app_mod.job_mgr,
        "fire_auth_lost_hook",
        lambda: calls.append(1),
    )
    monkeypatch.setattr(app_mod, "_TOKEN_VALID", None)
    monkeypatch.setattr(app_mod, "_TOKEN_GENERATION", None)
    monkeypatch.setattr(app_mod, "_AUTH_LOSS_NOTIFIED_GENERATIONS", set())

    asyncio.run(app_mod._probe_token())
    asyncio.run(app_mod._probe_token())

    assert app_mod._TOKEN_VALID is False
    assert app_mod._TOKEN_GENERATION == credentials.generation
    assert calls == [1]


def test_refresh_folds_into_parked_library_review(monkeypatch):
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import job_persistence, review_badges

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
    changed_scan = None
    try:
        webapp._fold_into_parked_library_review(scan)

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

        webapp._fold_into_parked_library_review(changed_scan)

        fresh_gap = next(c for c in parked.candidates if c["payload"].get("album_id") == "gap1")
        assert (fresh_gap["cid"], fresh_gap["seq"]) == old_identity
        assert fresh_gap["selected"] is True
        assert fresh_gap["detail"] == "gap-fill: 4 of 10 tracks missing"
        assert fresh_gap["payload"]["gap_fill"] == 4
        assert fresh_gap["payload"]["refresh_generation"] == "fresh"
        assert parked.execute_args["_library_review_generation"] == 124.0
        assert badge_calls == ["library"]
    finally:
        _remove_job(parked)
        _remove_job(scan)
        if changed_scan is not None:
            _remove_job(changed_scan)


def test_refresh_fold_refuses_to_publish_an_unsaved_review(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()

    parked = jm.Job(title="Library scan")
    parked.execute_kind = "library"
    parked.add_candidate(kind="album", title="Existing pick", artist="X",
                         payload={"album_id": "old"}, selected=True)
    parked.status = jm.JobStatus.AWAITING_REVIEW
    jm.registry.add(parked)

    scan = jm.Job(title="Library scan")
    scan.execute_kind = "library"
    scan.status = jm.JobStatus.SCANNING
    scan.add_candidate(kind="album", title="New find", artist="X",
                       payload={"album_id": "new"}, selected=False)
    jm.registry.add(scan)
    persist_locked = job_persistence._persist_locked
    monkeypatch.setattr(job_persistence, "_persist_locked", lambda _job: False)
    webapp._fold_into_parked_library_review(scan)

    assert [c["title"] for c in parked.candidates] == ["Existing pick"]
    assert parked.candidates[0]["selected"] is True
    assert [c["title"] for c in scan.candidates] == ["New find"]
    assert scan.status == jm.JobStatus.FAILED
    assert "couldn't be saved" in (scan.error or "")

    _remove_job(parked)
    _remove_job(scan)
    monkeypatch.setattr(job_persistence, "_persist_locked", persist_locked)
    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    jm.restore_jobs({"library": lambda _job, _args: lambda _j, _chosen: None})
    restored = jm.registry.get(parked.id)
    assert restored is not None
    assert [c["title"] for c in restored.candidates] == ["Existing pick"]
    assert restored.candidates[0]["selected"] is True






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
    # Hidden AFTER the scan built its candidate list: the stale-snapshot case.
    hidden_mod.hide(hidden_mod.SCOPE_MISSING, [("X", "Dismissed Mid-Scan", "")])
    try:
        webapp._fold_into_parked_library_review(scan)

        assert parked.candidates == []
    finally:
        hidden_mod.restore(hidden_mod.SCOPE_MISSING, ["X"])
        _remove_job(parked)
        _remove_job(scan)


def test_fold_skips_a_review_approved_mid_refresh(monkeypatch):
    """Approve flips the review out of AWAITING_REVIEW between scan finish
    and fold: the refresh must keep its candidates and park normally instead
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












def test_quality_shortfall_marks_history_until_the_job_is_opened(
        client, monkeypatch):
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence._reset_for_tests()
    job_persistence.init()

    job = jm.Job(title="Dummy", artist="Portishead", album_id="al1")
    job.status = jm.JobStatus.DONE
    job.attention = "quality"
    job.quality_shortfall = {
        "version": 1,
        "target": [24, 96000],
        "served": [16, 44100],
        "source": [24, 192000],
        "n_below": 3,
        "n_unknown": 1,
        "retried": True,
        "recovered": False,
        "effective_tier": 3,
    }
    job.log_lines = ["first retained diagnostic", "second retained diagnostic"]
    job.finished_at = time.time()
    job_persistence.persist(job)

    r = client.get("/queue/history")
    assert r.status_code == 200
    assert "ql-history-attention hidden" not in r.text

    r = client.get(f"/api/jobs/{job.id}/status")
    assert r.status_code == 200
    assert r.json()["log_lines"] == job.log_lines
    assert r.json()["quality_shortfall"] == job.quality_shortfall
    assert job_persistence.load_one(job.id)["attention"] == "quality"

    r = client.get(f"/jobs/{job.id}")
    assert r.status_code == 200
    assert all(line in r.text for line in job.log_lines)
    assert "24-bit / 96 kHz" in r.text
    assert "16-bit / 44.1 kHz" in r.text

    row = job_persistence.load_one(job.id)
    assert row["attention"] == ""
    assert row["log_lines"] == job.log_lines
    assert row["quality_shortfall"] == job.quality_shortfall
    r = client.get(f"/jobs/{job.id}")
    assert all(line in r.text for line in job.log_lines)
    assert "16-bit / 44.1 kHz" in r.text
    r = client.get("/queue/history")
    assert "ql-history-attention hidden" in r.text




def test_new_release_approve_parks_the_unticked_remnant(client, monkeypatch):
    """A new release stays in the New Releases review until it's downloaded or
    dismissed: approving 1 of 2 must park the other as its own new-release
    review, not consume it (the persistent baseline already recorded it, so
    nothing else would ever offer it again)."""
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(
        "qobuz_librarian.library.candidate_premise.validate_all",
        lambda _candidates: [],
    )

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
        rendered = client.get(f"/jobs/{job.id}")
        assert rendered.status_code == 200

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


def test_repair_approve_parks_the_unticked_remnant(client, monkeypatch):
    """Repair is a living review too, so approving one pick keeps the rest."""
    from qobuz_librarian.api.auth import credentials_from_values
    from qobuz_librarian.web import app as webapp

    credentials = credentials_from_values(
        "user", "token", source="streamrip")

    async def authorize(*_args, **_kwargs):
        return credentials

    monkeypatch.setattr(webapp, "_authorize_qobuz_for_web", authorize)
    monkeypatch.setattr(
        webapp, "_credential_generation_is_active", lambda _generation: True)
    monkeypatch.setattr(
        "qobuz_librarian.library.candidate_premise.validate_all",
        lambda _candidates: [],
    )
    monkeypatch.setitem(
        webapp._RESUME_EXECUTE,
        "repair",
        lambda _job, _args: (lambda _running, _chosen: None),
    )
    job = jm.Job(title="Repair scan")
    job.execute_kind = "repair"
    job.status = jm.JobStatus.AWAITING_REVIEW
    for title, selected in (("Repair now", True), ("Repair later", False)):
        job.add_candidate(
            "redownload",
            title,
            "Artist",
            payload={"album_id": title, "album_dir": f"/music/{title}"},
            selected=selected,
        )
    job._execute_fn = lambda _job, _chosen: None
    jm.registry.add(job)
    remnant = None
    try:
        response = client.post(
            f"/jobs/{job.id}/approve",
            data={"tab": ""},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert {candidate["title"] for candidate in job.candidates} == {
            "Repair now"
        }
        remnant = next(
            (
                candidate
                for candidate in jm.registry.awaiting_review()
                if candidate.id != job.id
                and candidate.execute_kind == "repair"
            ),
            None,
        )
        assert remnant is not None
        assert {candidate["title"] for candidate in remnant.candidates} == {
            "Repair later"
        }
    finally:
        _remove_job(job)
        if remnant is not None:
            _remove_job(remnant)






def test_partial_new_release_download_returns_to_the_nr_review(monkeypatch):
    """A New Releases download that lands only partly isn't downloaded, so the
    release goes back to the New Releases review (ticked, like a failure), and
    its remainder must NOT leak into the Library review as Gap Fill."""
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows, job_persistence

    _allow_legacy_candidate_execution(monkeypatch)

    monkeypatch.setattr(job_persistence, "_persist_locked", lambda _job: True)

    parked_nr = _inject_job(jm.JobStatus.AWAITING_REVIEW, "New-release check")
    parked_nr.execute_kind = "new_releases"
    parked_lib = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
    parked_lib.execute_kind = "library"
    running = _inject_job(jm.JobStatus.RUNNING, "New-release check")
    running.execute_kind = "new_releases"
    running.add_candidate(
        kind="album",
        title="Fresh Drop",
        artist="Abigail",
        payload={"album_id": "nr1"},
        selected=True,
    )
    chosen = list(running.candidates)
    monkeypatch.setattr(flows.cfg, "ARTIST_API_DELAY", 0)
    monkeypatch.setattr(
        flows, "get_album", lambda aid, _t: {"id": aid, "title": "Fresh Drop", "tracks_count": 10}
    )
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "_refresh_after_local_album_change", lambda *a, **k: None)
    monkeypatch.setattr(
        process_mod,
        "process_album",
        lambda *_a, **_k: {"imported": True, "n_ok": 7, "n_fail": 3, "result": "downloaded"},
    )
    try:
        flows.execute_albums(running, chosen, "tok")
        assert running.status == jm.JobStatus.FAILED
        assert running.summary == ("1 album only partly downloaded (some tracks are missing).")
        assert running.attention == "partial"
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
    job.add_candidate(
        kind="album",
        title="Kept",
        artist="Abigail",
        payload={"album_id": "k1", "year": 2020},
        selected=False,
    )
    cand = job.candidates[0]
    restored = []

    def hide_and_race(scope, specs, gap_fill=None):
        # The user's tick lands while the store write is in flight.
        cand["selected"] = True
        return len(list(specs))

    monkeypatch.setattr(hidden_mod, "hide", hide_and_race)
    monkeypatch.setattr(hidden_mod, "restore_rows", lambda scope, specs: restored.extend(specs))
    try:
        n = flows.dismiss_albums(job, "Abigail")
        assert n == 0
        assert [c["title"] for c in job.candidates] == ["Kept"]
        assert job.candidates[0]["selected"] is True
        assert restored == [("Abigail", "Kept", 2020)]

        # A route can pass its opening status check and then lose a race to
        # approval before the off-thread store write begins.
        cand["selected"] = False
        job.status = jm.JobStatus.PENDING
        assert flows.dismiss_albums(job, "Abigail") is None
        assert cand["selected"] is False
    finally:
        _remove_job(job)


def test_failed_dismiss_save_restores_only_the_new_exact_row(tmp_path, monkeypatch):
    from qobuz_librarian.library import hidden as hidden_mod
    from qobuz_librarian.web import flows, job_persistence

    monkeypatch.setattr(hidden_mod.cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    hidden_mod.hide(hidden_mod.SCOPE_MISSING, [("Weezer", "Weezer", "1994")])
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW, "Library scan")
    job.execute_kind = "library"
    job.add_candidate(
        kind="album",
        title="Weezer",
        artist="Weezer",
        payload={"album_id": "green-album", "year": 2001},
        selected=False,
    )
    monkeypatch.setattr(
        job_persistence,
        "persist_review_mutation",
        lambda _job, _mutate: (False, None),
    )
    try:
        assert flows.dismiss_albums(job, "Weezer") is False
        store = hidden_mod.load()
        assert (
            hidden_mod.is_hidden(hidden_mod.SCOPE_MISSING, "Weezer", "Weezer", store, year="1994")
            is True
        )
        assert (
            hidden_mod.is_hidden(hidden_mod.SCOPE_MISSING, "Weezer", "Weezer", store, year="2001")
            is False
        )
    finally:
        _remove_job(job)


def test_new_edition_download_folds_onto_an_identical_running_job():
    """ "Get this edition too" deliberately skips the owned-album fold, but two
    identical new-edition submits are the same tap twice, so the second folds
    onto the in-flight job instead of queueing a concurrent duplicate."""
    from qobuz_librarian.web import app as web_app

    running = _inject_job(jm.JobStatus.RUNNING, "Album, edition")
    running.album_id = "ALB9"
    running.execute_args = {"new_edition": True}
    other = _inject_job(jm.JobStatus.RUNNING, "Other, edition")
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




def test_stale_csrf_gets_a_readable_page_and_a_usable_token(client):
    # The old reply was text/plain "CSRF token missing or invalid" with no nav,
    # and because it wasn't HTML the middleware skipped minting a cookie too,
    # so the retry failed identically.
    client.cookies.clear()
    r = client.post("/settings/behavior", data={"form_complete": "1"},
                    follow_redirects=False)

    assert r.status_code == 403
    assert "CSRF token missing or invalid" not in r.text
    assert "ql_csrf" in r.headers.get("set-cookie", "")

    # And an htmx action gets told to reload rather than swallowing a 403.
    client.cookies.clear()
    r = client.post("/settings/behavior", data={"form_complete": "1"},
                    headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.headers.get("HX-Refresh") == "true"
    assert "ql_csrf" in r.headers.get("set-cookie", "")




def test_blank_login_does_not_spend_a_strike(client, monkeypatch):
    # Five blank taps must not lock the owner out of his own app for an hour.
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

    assert r.status_code == 401
    # The wait names what is actually left, not a fixed hour.
    assert "16 minutes" in r.text
    assert 'value="dink"' in r.text


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


def test_repair_page_does_not_deny_a_scan_it_is_showing(client, monkeypatch):
    """A failed run stays on /repair on purpose, with the launcher under it.
    The launcher's freshness line and resume offer were only ever computed on
    the idle branch, so that page rendered its own finish time above the words
    "No repair scan has finished yet" and never offered a resume."""
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import jobs as job_mgr

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(webapp, "_TOKEN_VALID", True)

    failed = job_mgr.Job(title="Repair scan")
    failed.execute_kind = "repair"
    failed.phase = "scan"
    failed.status = job_mgr.JobStatus.FAILED
    monkeypatch.setattr(webapp, "_repair_current_job", lambda: failed)
    monkeypatch.setattr(webapp, "_tool_last_run_age", lambda _kind: "2 days ago")
    monkeypatch.setattr(
        "qobuz_librarian.library.scan_checkpoint.load",
        lambda _kind: {"scanned": ["A", "B"], "candidates": []})

    page = client.get("/repair")
    assert page.status_code == 200
    assert 'action="/repair" method="post"' in page.text, (
        "an interrupted sweep must still offer resume")
    assert 'aria-label="Repair phase: scan failed"' in page.text
    phase = page.text.split('class="ql-repair-phase"', 1)[1].split("</div>", 1)[0]
    assert 'ql-repair-phase-label is-current is-error">Scan' in phase
    assert 'ql-repair-phase-label is-done">Review' not in phase


@pytest.mark.parametrize(
    ("status", "phase"),
    [
        (jm.JobStatus.RUNNING, "scan"),
        (jm.JobStatus.AWAITING_REVIEW, "review"),
    ],
)
def test_repair_scan_launcher_hidden_while_a_scan_owns_the_page(
        client, monkeypatch, status, phase):
    from qobuz_librarian.web import app as webapp

    job = jm.Job(title="Repair scan")
    job.execute_kind = "repair"
    job.phase = phase
    job.status = status
    if status is jm.JobStatus.AWAITING_REVIEW:
        job.add_candidate(
            "album", "Album", "Artist", payload={"album_id": "album"}
        )
    monkeypatch.setattr(webapp, "_repair_current_job", lambda: job)
    monkeypatch.setattr(webapp, "_qobuz_ready", lambda: True)

    page = client.get("/repair")

    assert page.status_code == 200
    assert 'action="/repair" method="post"' not in page.text


def test_interrupted_library_publication_stays_visible_without_write_authority(
        client, monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import generation_state, scan_checkpoint
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(
        cfg,
        "LIBRARY_GENERATION_STATE_FILE",
        tmp_path / "generation.json",
    )
    attempt = generation_state.begin_attempt()
    assert generation_state.commit_catalog_generation(attempt) is not None
    assert generation_state.library_publication_incomplete()

    monkeypatch.setattr(cfg, "AUTO_LIBRARY_SCAN", False)
    monkeypatch.setattr(webapp, "_qobuz_ready", lambda: True)
    monkeypatch.setattr(
        webapp,
        "_read_creds",
        lambda: {"auth_token": "saved-token", "user_id": "saved-user"},
    )
    monkeypatch.setattr(
        webapp,
        "_library_scan_state",
        lambda: {"ready": True, "count": 40, "message": ""},
    )
    monkeypatch.setattr(
        scan_checkpoint,
        "pending",
        lambda: {"kind": "missing", "done": 15},
    )

    library = client.get("/library")
    dashboard = client.get("/")

    assert library.status_code == 200
    assert "15 already checked, continues from there" in library.text
    assert dashboard.status_code == 200
    assert "15 artist" in dashboard.text
    assert ">Resume scan<" in dashboard.text
    assert ">Not now<" not in dashboard.text
    assert generation_state.load()["latest_attempt"]["status"] == "complete"


def test_web_lock_recovery_reconciles_library_before_restoring_jobs(
        monkeypatch):
    from types import SimpleNamespace

    from qobuz_librarian.library import generation_state
    from qobuz_librarian.web import app as webapp

    class Lease:
        closed = False

        def intact(self):
            return not self.closed

        def close(self):
            self.closed = True

    lease = Lease()
    events = []
    result = SimpleNamespace(status=SimpleNamespace(value="clear"))
    monkeypatch.setattr(webapp, "_RUN_LOCK_HANDLE", None)
    monkeypatch.setattr(
        webapp,
        "_record_startup_recovery",
        lambda authority: events.append(("queue", authority)) or result,
    )
    monkeypatch.setattr(
        generation_state,
        "reconcile_interrupted_library_publication",
        lambda authority: events.append(("library", authority)) or True,
    )
    monkeypatch.setattr(
        webapp,
        "_restore_jobs_once",
        lambda: events.append(("jobs", None)),
    )

    assert webapp._recover_under_web_run_lock(lease) is result
    assert events == [
        ("queue", lease),
        ("library", lease),
        ("jobs", None),
    ]
    assert webapp._RUN_LOCK_HANDLE is lease


def test_finished_download_marks_its_search_row_in_library(client):
    """A search row that launched a download used to go back to offering the
    same download the moment the job left the queue, because availability only
    ever reported what was still running. A finished album that landed in full
    is owned; one that came back with gaps is not."""
    complete = _inject_job(jm.JobStatus.DONE, title="Third")
    complete.album_id = "q123"
    complete.landed_complete = True
    partial = _inject_job(jm.JobStatus.DONE, title="Dummy")
    partial.album_id = "q456"
    try:
        owned = client.get("/api/search/availability").json()["owned"]
        assert "album-q123" in owned
        assert "album-q456" not in owned
    finally:
        _remove_job(complete)
        _remove_job(partial)


def test_downloading_the_collection_snapshot_serves_the_saved_file(
        client, tmp_path, monkeypatch):
    import json

    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import collection_snapshot

    monkeypatch.setattr(cfg, "COLLECTION_BACKUP_DIR", str(tmp_path / "backups"))
    # Nothing saved yet: the page says so rather than serving an empty file.
    missing = client.get("/collection/snapshot/download", follow_redirects=False)
    assert missing.status_code == 303

    document = {"format": collection_snapshot.FORMAT,
                "version": collection_snapshot.VERSION,
                "counts": {"artists": 1, "albums": 1, "tracks": 1},
                "artists": [{"name": "Artist", "albums": []}]}
    assert collection_snapshot.write_snapshot(document)[0] is True

    served = client.get("/collection/snapshot/download")
    assert served.status_code == 200
    assert "attachment" in served.headers["content-disposition"]
    assert json.loads(served.text)["counts"]["albums"] == 1


def test_a_manual_backup_reports_what_it_saved(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import collection_snapshot, scanner
    from qobuz_librarian.web import flows

    music = tmp_path / "music"
    (music / "Artist" / "Album").mkdir(parents=True)
    (music / "Artist" / "Album" / "01.flac").write_bytes(b"")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "COLLECTION_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(
        scanner, "read_album_dir",
        lambda d, walk_errors=None: [{"title": "One", "tracknumber": 1,
                                      "discnumber": 1, "isrc": "",
                                      "mb_trackid": "", "album": "Album"}])
    scanner.clear_scan_caches()

    job = jm.Job(title="Collection backup")
    flows.run_collection_snapshot(job)

    assert collection_snapshot.latest_path().is_file()
    assert "1 album" in job.summary


def _restore_stack(monkeypatch, tmp_path, albums):
    """An empty library and a catalogue that answers by album id."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import collection_restore

    music = tmp_path / "music"
    music.mkdir()
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
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


def test_a_file_that_is_not_a_backup_starts_nothing(client):
    before = len(jm.registry.all())

    response = client.post(
        "/collection/restore",
        files={"backup": ("notes.txt", b"nothing here", "text/plain")},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "HX-Redirect" not in response.headers
    assert len(jm.registry.all()) == before


def test_a_second_backup_upload_returns_the_open_restore(client, monkeypatch,
                                                          tmp_path):
    _restore_stack(monkeypatch, tmp_path, {})
    parked = jm.Job(title="Restore from backup")
    parked.execute_kind = "collection_restore"
    parked.status = jm.JobStatus.AWAITING_REVIEW
    jm.registry.add(parked)

    try:
        response = client.post(
            "/collection/restore",
            files={"backup": ("collection.json", _backup_file(),
                              "application/json")},
            headers={"HX-Request": "true"},
        )
        assert response.headers["HX-Redirect"] == f"/jobs/{parked.id}"
        assert len(jm.registry.all()) == 1
    finally:
        _remove_job(parked)


# ── Discover ──────────────────────────────────────────────────────────────────


def _discover_ready(monkeypatch, items=(), **overrides):
    """Point every Discover build at a finished feed so the route tests
    exercise the routes, not a background thread."""
    from qobuz_librarian.library import recommendations
    from qobuz_librarian.web import app as app_mod

    view = {"phase": "ready", "checked": 0, "total": 0, "error": "",
            "items": list(items), "built_at": time.time(), "stale": False}
    view.update(overrides)
    monkeypatch.setattr(app_mod, "_qobuz_ready", lambda: True)
    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(recommendations, "ensure_similar_feed",
                        lambda token: view)
    monkeypatch.setattr(recommendations, "ensure_search_feed",
                        lambda token, query: view)
    monkeypatch.setattr(recommendations, "library",
                        lambda **kw: recommendations.Library(
                            set(), [], ["A", "B"], "sig"))
    return view


def test_discover_is_absent_until_a_lastfm_key_is_set(client, monkeypatch):
    from qobuz_librarian import config as cfg

    monkeypatch.setattr(cfg, "LASTFM_API_KEY", "")
    for path in ("/discover", "/discover/genres", "/discover/search",
                 "/discover/favourites", "/discover/artist-albums?artist_id=1"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/"
    assert 'href="/discover"' not in client.get("/queue").text


def test_discover_appears_once_a_key_is_set(client, monkeypatch):
    from qobuz_librarian import config as cfg

    monkeypatch.setattr(cfg, "LASTFM_API_KEY", "k" * 32)
    _discover_ready(monkeypatch)
    assert client.get("/discover").status_code == 200
    assert 'href="/discover"' in client.get("/queue").text


def test_discover_lists_the_suggestions_and_what_caused_them(client, monkeypatch):
    from qobuz_librarian import config as cfg

    monkeypatch.setattr(cfg, "LASTFM_API_KEY", "k" * 32)
    _discover_ready(monkeypatch, items=[{
        "name": "Boards of Canada", "artist_id": "77",
        "seeds": ["Aphex Twin", "Autechre"], "score": 1.4,
        "cover": "https://static.qobuz.com/x.jpg",
        "albums": [{"id": "1"}, {"id": "2"}],
    }])
    body = client.get("/discover").text
    assert "Boards of Canada" in body
    assert "Aphex Twin" in body and "Autechre" in body
    # The row loads its albums when opened, not with the page.
    assert 'hx-get="/discover/artist-albums?artist_id=77' in body


def test_a_suggested_artists_albums_load_with_a_download_button(client, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import recommendations
    from qobuz_librarian.web import app as app_mod

    monkeypatch.setattr(cfg, "LASTFM_API_KEY", "k" * 32)
    monkeypatch.setattr(app_mod, "_qobuz_ready", lambda: True)
    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(recommendations, "artist_albums", lambda *a, **k: [
        {"id": "901", "title": "Geogaddi", "version": "", "artist": "BoC",
         "year": "2002", "tracks": 23, "maximum_bit_depth": 24,
         "maximum_sampling_rate": 44.1, "cover": ""},
        {"id": "902", "title": "Twoism", "version": "", "artist": "BoC",
         "year": "1995", "tracks": 9, "maximum_bit_depth": 16,
         "maximum_sampling_rate": 44.1, "cover": ""},
    ])
    body = client.get("/discover/artist-albums?artist_id=77&name=BoC").text
    assert 'name="album_id" value="901"' in body
    assert 'data-album-year="2002"' in body
    # Two decades are present, so the filter row is worth showing.
    assert 'data-decade="2000"' in body and 'data-decade="1990"' in body


def test_one_decade_gets_no_decade_filter(client, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import recommendations
    from qobuz_librarian.web import app as app_mod

    monkeypatch.setattr(cfg, "LASTFM_API_KEY", "k" * 32)
    monkeypatch.setattr(app_mod, "_qobuz_ready", lambda: True)
    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(recommendations, "artist_albums", lambda *a, **k: [
        {"id": "901", "title": "Geogaddi", "version": "", "artist": "BoC",
         "year": "2002", "tracks": 23, "maximum_bit_depth": 24,
         "maximum_sampling_rate": 44.1, "cover": ""}])
    body = client.get("/discover/artist-albums?artist_id=77&name=BoC").text
    assert "ql-decade-row" not in body


def test_genres_opens_on_the_top_tag_in_the_library(client, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import recommendations
    from qobuz_librarian.web import app as app_mod

    monkeypatch.setattr(cfg, "LASTFM_API_KEY", "k" * 32)
    monkeypatch.setattr(app_mod, "_qobuz_ready", lambda: True)
    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    tags_view = {"phase": "ready", "checked": 0, "total": 0, "error": "",
                 "items": ["shoegaze", "trip hop"], "built_at": time.time(),
                 "stale": False}
    asked = []

    def genre_feed(token, tag):
        asked.append(tag)
        return {"phase": "ready", "checked": 0, "total": 0, "error": "",
                "items": [{"id": "5", "title": "Souvlaki", "version": "",
                           "artist": "Slowdive", "year": "1993", "tracks": 10,
                           "maximum_bit_depth": 24,
                           "maximum_sampling_rate": 96, "cover": ""}],
                "built_at": time.time(), "stale": False}

    monkeypatch.setattr(recommendations, "ensure_library_tags",
                        lambda token: tags_view)
    monkeypatch.setattr(recommendations, "ensure_genre_feed", genre_feed)
    body = client.get("/discover/genres").text
    assert asked == ["shoegaze"]
    assert "Souvlaki" in body
    assert 'name="album_id" value="5"' in body
    client.get("/discover/genres?tag=trip+hop")
    assert asked[-1] == "trip hop"


def test_a_building_feed_asks_again_and_a_finished_one_stops(client, monkeypatch):
    from qobuz_librarian import config as cfg

    monkeypatch.setattr(cfg, "LASTFM_API_KEY", "k" * 32)
    _discover_ready(monkeypatch, phase="building", checked=7, total=90,
                    built_at=0)
    building = client.get("/discover").text
    assert 'hx-trigger="every 2s"' in building
    _discover_ready(monkeypatch)
    assert 'hx-trigger="every 2s"' not in client.get("/discover").text


def test_search_without_a_query_asks_lastfm_nothing(client, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import recommendations
    from qobuz_librarian.web import app as app_mod

    monkeypatch.setattr(cfg, "LASTFM_API_KEY", "k" * 32)
    monkeypatch.setattr(app_mod, "_qobuz_ready", lambda: True)
    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    asked = []

    def ensure(token, query):
        asked.append(query)
        return recommendations.feed_view("search:none", "sig")

    monkeypatch.setattr(recommendations, "ensure_search_feed", ensure)
    assert client.get("/discover/search").status_code == 200
    assert asked == [""]
