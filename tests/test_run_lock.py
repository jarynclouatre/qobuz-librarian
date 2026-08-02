import os

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
    # die() reflows to the terminal now, so a phrase can land across a line
    # break; compare against the message with the wrapping folded back out.
    flat = " ".join(message.split())
    assert stopped.value.code == 1
    assert lease.closed is True
    assert "interrupted library-folder move" in flat
    assert "exact relocation evidence changed" in flat
    assert "Paths needing attention" in flat
    assert all(str(path) in flat for path in affected_paths)
    assert "Post-import folder-move recovery needs attention" in flat
    assert "interrupted download" not in flat


