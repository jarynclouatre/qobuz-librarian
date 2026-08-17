import errno
from pathlib import Path


def _mark_badge_in_process(state_path, surface, attempting_lock, done):
    from pathlib import Path

    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import review_badges

    cfg.REVIEW_BADGE_STATE_FILE = Path(state_path)
    from qobuz_librarian import state_file

    lock_module = review_badges if hasattr(review_badges, "fcntl") else state_file
    real_flock = lock_module.fcntl.flock

    def report_lock_attempt(fd, operation):
        attempting_lock.set()
        return real_flock(fd, operation)

    lock_module.fcntl.flock = report_lock_attempt
    review_badges.mark_ready(surface, now=200.0)
    done.set()


def test_mark_ready_wins_over_previous_seen(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import review_badges

    monkeypatch.setattr(cfg, "REVIEW_BADGE_STATE_FILE",
                        tmp_path / "review-badges.json")

    review_badges.mark_ready("upgrade", now=500.0)
    first = review_badges.ready_generation("upgrade")
    review_badges.mark_seen("upgrade", first)
    assert review_badges.snapshot()["upgrade"] is False

    review_badges.mark_ready("upgrade", now=100.0)
    second = review_badges.ready_generation("upgrade")
    review_badges.mark_ready("upgrade", now=100.0)
    third = review_badges.ready_generation("upgrade")

    assert first < second < third
    review_badges.mark_seen("upgrade", second)
    assert review_badges.snapshot()["upgrade"] is True
    review_badges.mark_seen("upgrade", third)
    assert review_badges.snapshot()["upgrade"] is False


def test_generation_clear_keeps_a_newer_badge(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import review_badges

    monkeypatch.setattr(cfg, "REVIEW_BADGE_STATE_FILE",
                        tmp_path / "review-badges.json")

    review_badges.mark_ready("library", now=100.0)
    old = review_badges.ready_generation("library")
    review_badges.mark_ready("library", now=200.0)

    assert review_badges.clear_ready_if_generation("library", old) is False
    assert review_badges.snapshot()["library"] is True


def test_badge_write_failures_are_non_fatal(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import review_badges

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("blocks mkdir", encoding="utf-8")
    monkeypatch.setattr(cfg, "REVIEW_BADGE_STATE_FILE", blocker / "badges.json")

    review_badges.mark_ready("library")
    review_badges.mark_seen("library", review_badges.ready_generation("library"))
    review_badges.clear_ready("library")








def test_badge_updates_wait_for_another_process(monkeypatch, tmp_path):
    import fcntl
    import json
    import multiprocessing

    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import review_badges

    state_path = tmp_path / "review-badges.json"
    monkeypatch.setattr(cfg, "REVIEW_BADGE_STATE_FILE", state_path)
    lock_path = state_path.with_name(state_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    ctx = multiprocessing.get_context("spawn")
    attempting_lock = ctx.Event()
    done = ctx.Event()
    child = ctx.Process(
        target=_mark_badge_in_process,
        args=(str(state_path), "upgrade", attempting_lock, done),
    )

    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        child.start()
        try:
            assert attempting_lock.wait(timeout=5)
            assert not done.is_set()
            state = review_badges._empty()
            state["surfaces"]["library"]["ready_at"] = 100.0
            state_path.write_text(json.dumps(state), encoding="utf-8")
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    child.join(timeout=5)
    if child.is_alive():
        child.terminate()
        child.join(timeout=5)
    assert child.exitcode == 0
    assert done.is_set()
    snapshot = review_badges.snapshot()
    assert snapshot["library"] is True
    assert snapshot["upgrade"] is True


def test_stale_surface_does_not_light_its_dot(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import flows, review_badges

    monkeypatch.setattr(cfg, "REVIEW_BADGE_STATE_FILE",
                        tmp_path / "review-badges.json")
    monkeypatch.setattr(flows.upgrade_state, "has_visible_candidates",
                        lambda *a, **kw: True)
    current = {"upgrade": True}
    monkeypatch.setattr(flows.generation_state, "output_is_current",
                        lambda surface, **kw: current[surface])

    flows._sync_surface_badge("upgrade")
    assert review_badges.snapshot()["upgrade"] is True

    # The saved candidates are still there, but the Upgrade page will refuse to
    # show them, so the dot cannot keep promising a list.
    current["upgrade"] = False
    flows._sync_surface_badge("upgrade")
    assert review_badges.snapshot()["upgrade"] is False
