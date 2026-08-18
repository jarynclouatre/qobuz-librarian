import os
import signal

import pytest


def test_acquire_fsyncs_pid_to_disk(tmp_path, monkeypatch):
    lock_file = tmp_path / "run.lock"
    monkeypatch.setattr("qobuz_librarian.config.LOCK_FILE", lock_file)

    fsynced_fds = []
    orig_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: fsynced_fds.append(fd) or orig_fsync(fd))

    from qobuz_librarian import run_lock

    fp = run_lock.acquire()
    try:
        assert fp is not None
        assert fp.intact() is True
        assert fp.fileno() in fsynced_fds
        assert lock_file.read_text().strip() == str(os.getpid())
    finally:
        if fp is not None:
            fp.close()
    assert fp.intact() is False


def test_second_acquire_while_held_raises_lockbusy_with_holder_pid(tmp_path, monkeypatch):
    lock_file = tmp_path / "run.lock"
    monkeypatch.setattr("qobuz_librarian.config.LOCK_FILE", lock_file)

    from qobuz_librarian import run_lock

    held = run_lock.acquire()
    try:
        assert held is not None
        with pytest.raises(run_lock.LockBusy) as caught:
            run_lock.acquire()
        assert caught.value.pid == str(os.getpid())
    finally:
        held.close()

    again = run_lock.acquire()
    assert again is not None
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


def test_acquire_refuses_a_symlink_without_touching_its_target(
        tmp_path, monkeypatch):
    victim = tmp_path / "victim"
    victim.write_text("keep me", encoding="utf-8")
    lock_file = tmp_path / "run.lock"
    lock_file.symlink_to(victim)
    monkeypatch.setattr("qobuz_librarian.config.LOCK_FILE", lock_file)

    from qobuz_librarian import run_lock

    assert run_lock.acquire() is None
    assert victim.read_text(encoding="utf-8") == "keep me"


def test_replacing_the_lock_name_cannot_admit_a_second_writer(
        tmp_path, monkeypatch):
    lock_file = tmp_path / "run.lock"
    monkeypatch.setattr("qobuz_librarian.config.LOCK_FILE", lock_file)

    from qobuz_librarian import run_lock

    first = run_lock.acquire()
    assert first is not None
    lock_file.unlink()
    try:
        assert first.intact() is False
        with pytest.raises(run_lock.LockBusy):
            run_lock.acquire()
    finally:
        first.close()


def test_acquire_degrades_to_none_when_flock_unsupported(tmp_path, monkeypatch, caplog):
    import errno
    import fcntl
    import logging

    lock_file = tmp_path / "run.lock"
    monkeypatch.setattr("qobuz_librarian.config.LOCK_FILE", lock_file)

    def no_flock(fd, op):
        raise OSError(errno.ENOLCK, "no locks available")
    monkeypatch.setattr(fcntl, "flock", no_flock)

    from qobuz_librarian import run_lock

    with caplog.at_level(logging.WARNING, logger="qobuz_librarian"):
        assert run_lock.acquire() is None
    warning = next(
        r.getMessage() for r in caplog.records
        if "single-instance lock" in r.getMessage()
    )
    assert "data folder supports file locking" in warning
    assert "accept" not in warning


def test_cli_refuses_to_run_when_the_lock_is_unavailable(monkeypatch):
    from qobuz_librarian import cli, run_lock

    monkeypatch.setattr(run_lock, "acquire", lambda: None)

    with pytest.raises(SystemExit) as stopped:
        cli.acquire_run_lock()
    assert stopped.value.code == 1


def test_cli_reconciles_library_publication_under_its_run_lock(monkeypatch):
    from qobuz_librarian import cli, run_lock
    from qobuz_librarian.library import generation_state
    from qobuz_librarian.queue.startup_recovery import (
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
    events = []
    monkeypatch.setattr(run_lock, "acquire", lambda: lease)
    monkeypatch.setattr(
        cli,
        "_recover_startup_queue",
        lambda authority: events.append(("queue", authority))
        or StartupRecoveryResult(StartupRecoveryStatus.CLEAR),
    )
    monkeypatch.setattr(
        generation_state,
        "reconcile_interrupted_library_publication",
        lambda authority: events.append(("library", authority)) or True,
    )

    assert cli.acquire_run_lock() is lease
    assert events == [("queue", lease), ("library", lease)]
    assert lease.closed is False




def test_cli_folder_move_recovery_pause_names_cause_and_exact_paths(
        monkeypatch, capsys, tmp_path):
    from qobuz_librarian import cli, run_lock
    from qobuz_librarian.library.post_import_relocation import (
        RelocationRecoveryResult,
        RelocationRecoveryStatus,
    )
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )

    class Lease:
        closed = False

        def intact(self):
            return not self.closed

        def close(self):
            self.closed = True

    affected_paths = (
        tmp_path / "music" / "Artist One" / "Album One",
        tmp_path / "music" / "Artist Two" / "Album Two",
    )
    lease = Lease()
    monkeypatch.setattr(run_lock, "acquire", lambda: lease)
    monkeypatch.setattr(
        cli,
        "_recover_startup_queue",
        lambda authority: StartupRecoveryResult(
            StartupRecoveryStatus.ATTENTION_REQUIRED,
            reason="post-import-relocation-unsettled",
            post_import_relocation=RelocationRecoveryResult(
                RelocationRecoveryStatus.ATTENTION_REQUIRED,
                "exact relocation evidence changed",
                affected_paths,
            ),
        ),
    )

    with pytest.raises(SystemExit) as stopped:
        cli.acquire_run_lock()

    message = capsys.readouterr().err
    # The prose reflows to the terminal, so a phrase can land across a line
    # break - compare prose with the wrapping folded back out. The paths print
    # raw and must survive verbatim, doubled spaces and all.
    flat = " ".join(message.split())
    assert stopped.value.code == 1
    assert lease.closed is True
    assert "interrupted library-folder move" in flat
    assert "exact relocation evidence changed" in flat
    assert "Paths needing attention" in flat
    assert all(str(path) in message for path in affected_paths)
    assert "Post-import folder-move recovery needs attention" in flat
    assert "interrupted download" not in flat




def test_cli_carries_on_when_a_refused_settlement_cleared_the_recovery(
        monkeypatch, capsys):
    """Retrying a blocked item that already imported gets refused - the item is
    no longer a pre-launch abort - but the refusal still parks its stranded
    staging, which clears the recovery. Dying on the stale verdict made the run
    abort for nothing.
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
    item = SimpleNamespace(operation_id="op-1", item_id="item-1")
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
            "Beets may have started or changed the library, so this item "
            "remains blocked.",
        )

    monkeypatch.setattr(run_lock, "acquire", lambda: lease)
    monkeypatch.setattr(cli, "_recover_startup_queue", _recover)
    monkeypatch.setattr(
        startup_recovery,
        "blocked_settlement_binding",
        lambda result: (
            item,
            "Autechre - Anvil Vapre",
            startup_recovery.SETTLEABLE_PRELAUNCH,
        ),
    )
    monkeypatch.setattr(startup_recovery, "settle_blocked_item", _settle)
    monkeypatch.setattr("builtins.input", lambda _prompt: "r")

    assert cli.acquire_run_lock() is lease
    assert lease.closed is False
    assert "remains blocked" not in capsys.readouterr().out


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

    prompts = []

    monkeypatch.setattr(run_lock, "acquire", lambda: lease)
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

    def _input(prompt):
        prompts.append(prompt)
        return "c"

    monkeypatch.setattr("builtins.input", _input)

    with caplog.at_level("INFO", logger="qobuz_librarian"):
        assert cli.acquire_run_lock() is lease
    assert lease.closed is False
    asked = " ".join(" ".join(prompts).split())
    assert "left something behind in the staging folder" in asked
    # Nothing can be re-run and the saved entry is not what is holding things
    # up, so naming a retry or a discard would offer the same thing twice.
    assert "discard" not in asked.lower()
    assert "retry" not in asked.lower()
    assert "Cleared the leftover" in caplog.text


def test_clearing_a_leftover_is_not_reported_as_a_failure(
        monkeypatch, capsys, caplog):
    """Clearing a staged leftover can leave the rest of the recovery to
    reconcile on the next pass. Judging that by the process-wide status printed
    "could not be verified safely" over a decision that had just succeeded, and
    sent the user hunting a path or permission problem that did not exist.
    """
    from types import SimpleNamespace

    import pytest as _pytest

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
    item = SimpleNamespace(operation_id="op-7", item_id="item-7")
    settled = {"done": False}

    def _entry(phase):
        return SimpleNamespace(
            operation_id="op-7", item_id="item-7", phase=phase, mode="cli",
        )

    def _recover(_authority):
        # Still attention_required either way - but after the settlement this
        # item is no longer the blocked one.
        return StartupRecoveryResult(
            StartupRecoveryStatus.ATTENTION_REQUIRED,
            items=(_entry(
                queue_state.QueuePhase.RESOLVING if settled["done"]
                else queue_state.QueuePhase.BLOCKED),),
            reason="queue-item-blocked",
        )

    def _settle(**_kwargs):
        settled["done"] = True
        return BlockedItemSettlementResult(
            BlockedItemSettlementStatus.BLOCKED,
            "Beets may have started or changed the library, so this item "
            "remains blocked.",
        )

    monkeypatch.setattr(run_lock, "acquire", lambda: lease)
    monkeypatch.setattr(cli, "_recover_startup_queue", _recover)
    monkeypatch.setattr(
        startup_recovery,
        "blocked_settlement_binding",
        lambda result: (
            item,
            "Agalloch - The Serpent & The Sphere",
            startup_recovery.SETTLEABLE_STAGED_LEFTOVER,
        ),
    )
    monkeypatch.setattr(startup_recovery, "settle_blocked_item", _settle)
    monkeypatch.setattr("builtins.input", lambda _prompt: "c")

    with _pytest.raises(SystemExit) as stopped:
        cli.acquire_run_lock()

    said = " ".join((capsys.readouterr().err + " " + caplog.text).split())
    assert stopped.value.code == 1
    assert "Cleared the leftover" in said
    assert "Restart Qobuz Librarian to finish settling it" in said
    assert "could not be verified safely" not in said


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

    monkeypatch.setattr(run_lock, "acquire", lambda: lease)
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
    assert "Cleared the leftover" in caplog.text
    assert "could not be verified safely" not in caplog.text
