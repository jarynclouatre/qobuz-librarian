import errno
from pathlib import Path


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
