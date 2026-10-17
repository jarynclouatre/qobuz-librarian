import os
import signal

import pytest


def test_second_acquire_while_held_raises_lockbusy_with_holder_pid(tmp_path, monkeypatch):
    # A link above the data folder (a dotfile manager's ~/.local/share, or
    # DATA_DIR on a linked mount) still holds the lock.
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    lock_file = tmp_path / "link" / "run.lock"
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
