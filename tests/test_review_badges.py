import errno
from pathlib import Path


def test_upgrade_dot_lights_only_for_candidates_it_has_not_offered(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import flows, review_badges

    monkeypatch.setattr(cfg, "REVIEW_BADGE_STATE_FILE",
                        tmp_path / "review-badges.json")
    saved = {"complete": True,
             "candidates": [{"artist": "Feist", "title": "Metals"}]}
    monkeypatch.setattr(flows.upgrade_state, "load", lambda: saved)
    monkeypatch.setattr(flows.upgrade_state, "visible_candidates",
                        lambda state, hidden=None: state["candidates"])
    current = {"upgrade": True}
    monkeypatch.setattr(flows.generation_state, "output_is_current",
                        lambda surface, **kw: current[surface])

    flows._sync_surface_badge("upgrade")
    assert review_badges.snapshot()["upgrade"] is True
    review_badges.mark_seen("upgrade", review_badges.ready_generation("upgrade"))

    # A quick refresh or a download that leaves the same candidates has
    # nothing new to point at.
    flows._sync_surface_badge("upgrade")
    assert review_badges.snapshot()["upgrade"] is False

    saved["candidates"].append({"artist": "Feist", "title": "Pleasure"})
    flows._sync_surface_badge("upgrade")
    assert review_badges.snapshot()["upgrade"] is True

    # The saved candidates are still there, but the Upgrade page will refuse to
    # show them, so the dot cannot keep promising a list.
    current["upgrade"] = False
    flows._sync_surface_badge("upgrade")
    assert review_badges.snapshot()["upgrade"] is False
