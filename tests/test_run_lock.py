import os
import signal

import pytest


def test_second_acquire_while_held_raises_lockbusy_with_holder_pid(tmp_path, monkeypatch):
    lock_file = tmp_path / "run.lock"
    monkeypatch.setattr("qobuz_librarian.config.LOCK_FILE", lock_file)

    from qobuz_librarian import run_lock

    held = run_lock.acquire("web")
    try:
        assert held is not None
        with pytest.raises(run_lock.LockBusy) as caught:
            run_lock.acquire()
        assert caught.value.pid == str(os.getpid())
        assert caught.value.holder == "web"
    finally:
        held.close()

    again = run_lock.acquire()
    assert again is not None
    # Deleting the lock file does not admit a second writer.
    lock_file.unlink()
    assert again.intact() is False
    with pytest.raises(run_lock.LockBusy):
        run_lock.acquire()
    again.close()


def test_sigterm_releases_the_process_lock_for_the_next_writer(
        tmp_path, monkeypatch):
    lock_file = tmp_path / "run.lock"
    monkeypatch.setattr("qobuz_librarian.config.LOCK_FILE", lock_file)

    from qobuz_librarian import run_lock

    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        lease = run_lock.acquire()
        if lease is None:
            os._exit(90)
        os.write(write_fd, b"ready")
        os.close(write_fd)
        signal.pause()
        os._exit(91)

    os.close(write_fd)
    try:
        assert os.read(read_fd, 5) == b"ready"
        os.kill(child, signal.SIGTERM)
        _pid, status = os.waitpid(child, 0)
        child = None
        assert os.waitstatus_to_exitcode(status) == -signal.SIGTERM

        lease = run_lock.acquire()
        assert lease is not None
        lease.close()
    finally:
        os.close(read_fd)
        if child is not None:
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)


def test_acquire_degrades_to_none_when_flock_unsupported(tmp_path, monkeypatch):
    import errno
    import fcntl

    lock_file = tmp_path / "run.lock"
    monkeypatch.setattr("qobuz_librarian.config.LOCK_FILE", lock_file)

    def no_flock(fd, op):
        raise OSError(errno.ENOLCK, "no locks available")
    monkeypatch.setattr(fcntl, "flock", no_flock)

    from qobuz_librarian import cli, run_lock

    assert run_lock.acquire() is None
    # The terminal then refuses to run unguarded.
    with pytest.raises(SystemExit) as stopped:
        cli.acquire_run_lock()
    assert stopped.value.code == 1


def test_a_staged_leftover_is_offered_a_decision_in_the_terminal(
        monkeypatch, caplog):
    """A download that imported and stranded a file in staging pauses every
    download and scan, and a restart does not clear it, so the terminal has to
    offer the decision itself.
    """
    from types import SimpleNamespace

    from qobuz_librarian import cli, run_lock
    from qobuz_librarian.queue import startup_recovery
    from qobuz_librarian.queue.startup_recovery import (
        BlockedItemSettlementResult,
        BlockedItemSettlementStatus,
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )

    class Lease:
        closed = False

        def intact(self):
            return not self.closed

        def close(self):
            self.closed = True

    lease = Lease()
    item = SimpleNamespace(operation_id="op-9", item_id="item-9")
    settled = {"done": False}

    def _recover(_authority):
        if settled["done"]:
            return StartupRecoveryResult(StartupRecoveryStatus.CLEAR)
        return StartupRecoveryResult(
            StartupRecoveryStatus.ATTENTION_REQUIRED,
            reason="queue-item-blocked",
        )

    def _settle(**_kwargs):
        settled["done"] = True
        return BlockedItemSettlementResult(
            BlockedItemSettlementStatus.BLOCKED,
            "This item has no exact pre-launch Beets state to settle.",
        )

    monkeypatch.setattr(run_lock, "acquire", lambda _holder: lease)
    monkeypatch.setattr(cli, "_recover_startup_queue", _recover)
    monkeypatch.setattr(
        startup_recovery,
        "blocked_settlement_binding",
        lambda result: (
            item,
            "Agalloch - The White EP",
            startup_recovery.SETTLEABLE_STAGED_LEFTOVER,
        ),
    )
    monkeypatch.setattr(startup_recovery, "settle_blocked_item", _settle)

    monkeypatch.setattr("builtins.input", lambda _prompt: "c")

    with caplog.at_level("INFO", logger="qobuz_librarian"):
        assert cli.acquire_run_lock() is lease
    assert lease.closed is False
    assert "✓" in caplog.text


def test_a_settled_leftover_does_not_report_the_stale_verdict(
        monkeypatch, caplog):
    """Clearing a staged leftover succeeds, then the whole recovery reconciles
    a pass later. Judging the run by the read taken before the settlement
    printed "could not be verified safely" and exited 1 over a clear that had
    just worked, pointing the user at a recovery reason nothing had recorded.
    """
    from types import SimpleNamespace

    from qobuz_librarian import cli, run_lock
    from qobuz_librarian.queue import journal as queue_state
    from qobuz_librarian.queue import startup_recovery
    from qobuz_librarian.queue.startup_recovery import (
        BlockedItemSettlementResult,
        BlockedItemSettlementStatus,
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )

    class Lease:
        closed = False

        def intact(self):
            return not self.closed

        def close(self):
            self.closed = True

    lease = Lease()
    item = SimpleNamespace(operation_id="op-3", item_id="item-3")
    reads = {"n": 0}

    def _recover(_authority):
        reads["n"] += 1
        if reads["n"] == 1:
            return StartupRecoveryResult(
                StartupRecoveryStatus.ATTENTION_REQUIRED,
                items=(SimpleNamespace(
                    operation_id="op-3", item_id="item-3", mode="cli",
                    phase=queue_state.QueuePhase.BLOCKED),),
                reason="queue-item-blocked",
            )
        if reads["n"] == 2:
            return StartupRecoveryResult(
                StartupRecoveryStatus.ATTENTION_REQUIRED,
                items=(SimpleNamespace(
                    operation_id="op-3", item_id="item-3", mode="cli",
                    phase=queue_state.QueuePhase.RESOLVING),),
                reason="queue-item-blocked",
            )
        return StartupRecoveryResult(StartupRecoveryStatus.CLEAR)

    def _settle(**_kwargs):
        return BlockedItemSettlementResult(
            BlockedItemSettlementStatus.RETRYABLE, "",
        )

    monkeypatch.setattr(run_lock, "acquire", lambda _holder: lease)
    monkeypatch.setattr(cli, "_recover_startup_queue", _recover)
    monkeypatch.setattr(
        startup_recovery,
        "blocked_settlement_binding",
        lambda result: (
            item,
            "Aphex Twin - Music From The Merch Desk",
            startup_recovery.SETTLEABLE_STAGED_LEFTOVER,
        ),
    )
    monkeypatch.setattr(startup_recovery, "settle_blocked_item", _settle)
    monkeypatch.setattr("builtins.input", lambda _prompt: "c")

    with caplog.at_level("INFO", logger="qobuz_librarian"):
        assert cli.acquire_run_lock() is lease
    assert lease.closed is False
    assert "✓" in caplog.text
    assert not any(record.levelname == "ERROR" for record in caplog.records)
