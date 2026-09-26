"""Fault-injection tests for the Web job durability boundary."""

import queue
import sqlite3

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian.web import job_persistence
from qobuz_librarian.web import jobs as jm
from qobuz_librarian.web.jobs import Job, JobStatus

_REAL_ADMIT = job_persistence.admit
_REAL_ADMIT_REVIEW = job_persistence.admit_review_transition


class _FailCommitOnce:
    """Forward to SQLite but fail the first commit after its write began."""

    def __init__(self, connection):
        self._connection = connection
        self._fail_commit = True

    @property
    def in_transaction(self):
        return self._connection.in_transaction

    def execute(self, *args, **kwargs):
        return self._connection.execute(*args, **kwargs)

    def commit(self):
        if self._fail_commit:
            self._fail_commit = False
            raise sqlite3.OperationalError("injected commit failure")
        return self._connection.commit()

    def rollback(self):
        return self._connection.rollback()

    def close(self):
        return self._connection.close()

    def __getattr__(self, name):
        return getattr(self._connection, name)


def test_failed_job_commit_cannot_ride_a_later_successful_commit(
        monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    connection = _FailCommitOnce(job_persistence._conn)
    monkeypatch.setattr(job_persistence, "_conn", connection)

    rejected = Job(title="Rejected admission")
    assert job_persistence.ready_for_admission() is True
    assert _REAL_ADMIT(rejected) is False
    assert job_persistence.ready_for_admission() is False
    assert connection.in_transaction is False

    accepted = Job(title="Accepted admission")
    assert _REAL_ADMIT(accepted) is True
    assert job_persistence.ready_for_admission() is True

    observer = sqlite3.connect(cfg.DATA_DIR / "jobs.db")
    try:
        saved_ids = {
            row[0] for row in observer.execute("SELECT id FROM jobs").fetchall()
        }
    finally:
        observer.close()

    assert rejected.id not in saved_ids
    assert accepted.id in saved_ids


@pytest.mark.parametrize("kind", ["library", "new_releases"])
def test_cancel_approved_review_preserves_picks_after_reload(
        monkeypatch, tmp_path, kind):
    from qobuz_librarian.web import job_runs, routes_jobs

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    job_persistence._reset_for_tests()
    job_persistence.init()
    monkeypatch.setattr(job_persistence, "admit", _REAL_ADMIT)
    monkeypatch.setattr(job_persistence, "admit_review_transition", _REAL_ADMIT_REVIEW)
    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    work = queue.Queue()
    monkeypatch.setattr(jm, "_scan_queue", work)

    def scan(job):
        job.add_candidate("album", "Picked", payload={"album_id": "picked"},
                          selected=False)
        job.add_candidate("album", "Left parked", payload={"album_id": "parked"},
                          selected=False)

    job = jm.Job(title="Library scan", execute_kind=kind)
    assert jm.submit_scan(job, scan, lambda *_: None) is job
    queued, run = work.get_nowait()
    run(queued)
    assert job.status == jm.JobStatus.AWAITING_REVIEW
    assert job.set_selected(job.candidates[0]["cid"], True)
    assert jm.approve(
        job, split_review=lambda review: routes_jobs._build_unapproved_review(review, ""),
    ) is True
    assert job.status == jm.JobStatus.PENDING
    assert jm.request_cancel(job) is True

    reviews = jm.registry.awaiting_review()
    assert reviews == [job]
    expected = [("picked", True), ("parked", False)]
    assert [(c["payload"]["album_id"], c["selected"])
            for c in job.candidates] == expected
    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    jm.restore_jobs({kind: job_runs._resume_album_download})
    reviews = jm.registry.awaiting_review()
    assert len(reviews) == 1
    assert [(c["payload"]["album_id"], c["selected"])
            for c in reviews[0].candidates] == expected


def test_a_provider_error_never_reaches_the_stored_job_record(
        monkeypatch, tmp_path):
    """A Qobuz call carries the account email and the auth token in its query
    string, so a failed call reported as the URL it called would put a working
    credential in the activity log and the archive. Records written before the
    masking existed are cleaned on the next start."""
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    leak = ("Cannot connect to https://example.test/api/user/login"
            "?user_id=nobody@example.test&user_auth_token=NOT-A-REAL-TOKEN-0000")
    job = Job(title="Download")
    job.push_line(leak)
    assert "NOT-A-REAL-TOKEN-0000" not in "\n".join(job.log_lines)
    assert "nobody@example.test" not in "\n".join(job.log_lines)

    job.status = JobStatus.FAILED
    assert _REAL_ADMIT(job) is True
    observer = sqlite3.connect(cfg.DATA_DIR / "jobs.db")
    try:
        observer.execute("UPDATE jobs SET error=?, summary=? WHERE id=?",
                         (leak, leak, job.id))
        observer.commit()
    finally:
        observer.close()

    assert job_persistence.scrub_stored_secrets() == 1
    observer = sqlite3.connect(cfg.DATA_DIR / "jobs.db")
    try:
        stored = observer.execute(
            "SELECT error, summary FROM jobs WHERE id=?", (job.id,)).fetchone()
    finally:
        observer.close()
    assert "NOT-A-REAL-TOKEN-0000" not in "".join(stored)
    assert "nobody@example.test" not in "".join(stored)
