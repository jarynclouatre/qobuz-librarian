"""Focused guards for durable post-import action integration."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from qobuz_librarian.integrations.beets import ManagedCarrierRetirementOutcome
from qobuz_librarian.queue import durable_runner, journal, startup_recovery
from qobuz_librarian.web import flows


class _StopAfterPolicyCapture(RuntimeError):
    pass


def test_new_durable_item_freezes_multi_artist_filing_policy(monkeypatch):
    # A web download files a collaboration under its main artist by default.
    saved = SimpleNamespace(operation_id="a" * 64)
    saved_item = SimpleNamespace(
        item_id="b" * 64,
        phase=journal.QueuePhase.PENDING,
    )
    monkeypatch.setattr(durable_runner, "_require_authority", lambda _value: None)
    monkeypatch.setattr(durable_runner, "_require_current_plan", lambda *_args: None)
    monkeypatch.setattr(
        durable_runner,
        "_claim_pending_item",
        lambda *_args, **_kwargs: (saved, saved_item),
    )
    monkeypatch.setattr(
        durable_runner,
        "initial_completion_input",
        lambda *_args: {"frozen": True},
    )
    monkeypatch.setattr(durable_runner, "isolated_staging_run_names", lambda: ())

    def capture_policy(current, item_id, phase, **values):
        assert current is saved
        assert item_id == saved_item.item_id
        assert phase is journal.QueuePhase.ACTIVE
        assert values["multi_artist_filing"] is True
        raise _StopAfterPolicyCapture

    monkeypatch.setattr(
        durable_runner.queue_state,
        "transition_journal_item",
        capture_policy,
    )

    with pytest.raises(_StopAfterPolicyCapture):
        durable_runner.execute_durable_new_album(
            [],
            {},
            flows.build_args(),
            plan=object(),
            origin=object(),
            mode="test",
            authority=object(),
        )


def test_startup_retirement_uses_action_finalizer(monkeypatch):
    retirement = SimpleNamespace(item_id="e" * 64)
    current = SimpleNamespace(
        operation_id="f" * 64,
        items=(),
        retirements=(retirement,),
    )
    state = {"journal": current}
    callback = object()

    monkeypatch.setattr(startup_recovery, "_require_authority", lambda _value: None)
    monkeypatch.setattr(
        startup_recovery,
        "reconcile_post_import_relocations",
        lambda **_kwargs: SimpleNamespace(
            status=startup_recovery.RelocationRecoveryStatus.CLEAR
        ),
    )
    monkeypatch.setattr(
        startup_recovery,
        "_load_namespace",
        lambda _authority: (
            ()
            if state["journal"] is None
            else (
                SimpleNamespace(
                    status=journal.QueueLoadStatus.READY,
                    journal=state["journal"],
                ),
            )
        ),
    )
    monkeypatch.setattr(
        startup_recovery,
        "_recover_active_library_backups",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        startup_recovery,
        "_unclaimed_staging_run_names",
        lambda *_args: (),
    )
    monkeypatch.setattr(
        startup_recovery,
        "_recover_staging_references",
        lambda *_args: False,
    )

    def finalize(saved, item_id, *, authority, acknowledge_completion):
        assert saved is current
        assert item_id == retirement.item_id
        assert authority == "authority"
        assert acknowledge_completion is callback
        state["journal"] = SimpleNamespace(
            operation_id=current.operation_id,
            items=(),
            retirements=(),
        )
        return (
            state["journal"],
            Path("/music/Various Artists/Album"),
            SimpleNamespace(
                outcome=ManagedCarrierRetirementOutcome.RETIRED
            ),
        )

    def clear(operation_id):
        assert operation_id == current.operation_id
        state["journal"] = None

    monkeypatch.setattr(
        startup_recovery,
        "finalize_carrier_retirement",
        finalize,
    )
    monkeypatch.setattr(startup_recovery.queue_state, "clear_queue_journal", clear)

    result = startup_recovery.recover_startup_state(
        authority="authority",
        acknowledge_completion=callback,
    )

    assert result.status is startup_recovery.StartupRecoveryStatus.CLEAR
