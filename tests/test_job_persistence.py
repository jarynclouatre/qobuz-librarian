"""Fault-injection tests for the Web job durability boundary."""

import sqlite3

from qobuz_librarian import config as cfg
from qobuz_librarian.web import job_persistence
from qobuz_librarian.web.jobs import Job, JobStatus


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
    assert job_persistence.persist(rejected) is False
    assert connection.in_transaction is False

    accepted = Job(title="Accepted admission")
    assert job_persistence.persist(accepted) is True

    observer = sqlite3.connect(cfg.DATA_DIR / "jobs.db")
    try:
        saved_ids = {
            row[0] for row in observer.execute("SELECT id FROM jobs").fetchall()
        }
    finally:
        observer.close()

    assert rejected.id not in saved_ids
    assert accepted.id in saved_ids
