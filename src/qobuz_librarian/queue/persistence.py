"""Compatibility entry points and the CLI resume prompt for queue journals."""

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost
from qobuz_librarian.queue.journal import (
    JournalItem,
    QueueJournal,
    QueueJournalBlocked,
    QueueJournalError,
    QueueLoad,
    QueueLoadStatus,
    QueuePhase,
    RecoveryReference,
    _deserialize_queue_item,
    _serialize_queue_item,
    clear_pending_queue,
    clear_queue_journal,
    commit_completed_item_removal,
    commit_recovered_completed_item_removal,
    create_queue_journal,
    list_queue_journals,
    load_queue_journal,
    process_carrier_retirement,
    save_pending_queue,
    save_queue_journal,
    transition_journal_item,
)
from qobuz_librarian.ui_cli.colors import C, fmt
from qobuz_librarian.ui_cli.logging import log

__all__ = [
    "JournalItem",
    "QueueJournal",
    "QueueJournalBlocked",
    "QueueJournalError",
    "QueueLoad",
    "QueueLoadStatus",
    "QueuePhase",
    "RecoveryReference",
    "_deserialize_queue_item",
    "_serialize_queue_item",
    "clear_pending_queue",
    "clear_queue_journal",
    "commit_completed_item_removal",
    "commit_recovered_completed_item_removal",
    "create_queue_journal",
    "load_pending_queue",
    "load_queue_journal",
    "list_queue_journals",
    "offer_resume_pending_queue",
    "offer_resume_startup_recovery",
    "process_carrier_retirement",
    "save_pending_queue",
    "save_queue_journal",
    "transition_journal_item",
]


def load_pending_queue():
    """Load typed v2 state while preserving tuple unpacking for old callers."""
    loaded = load_queue_journal()
    if loaded.status is QueueLoadStatus.BLOCKED:
        location = loaded.paths[0] if len(loaded.paths) == 1 else cfg.QUEUE_JOURNAL_DIR
        log.info(fmt(
            C.YELLOW,
            f"  ⚠  Saved queue state at {location} needs attention "
            f"({loaded.reason or 'invalid journal'}); it was left unchanged.",
        ))
    return loaded


def _load_startup_recovery_queue(recovery):
    """Bind one typed CLI recovery result back to its exact journal."""
    from qobuz_librarian.completion import (
        CompletionOriginKind,
        RecoveryOwner,
        normalise_album_id,
        parse_completion_input_record,
    )
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryAction,
        StartupRecoveryItem,
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )

    if (
        type(recovery) is not StartupRecoveryResult
        or recovery.status is not StartupRecoveryStatus.RESUME_REQUIRED
        or type(recovery.items) is not tuple
        or not recovery.items
        or any(type(item) is not StartupRecoveryItem for item in recovery.items)
    ):
        raise QueueJournalBlocked("CLI recovery state is unavailable")

    operations = {
        (item.operation_id, item.mode)
        for item in recovery.items
    }
    if len(operations) != 1:
        raise QueueJournalBlocked(
            "CLI recovery does not name one exact queue operation"
        )
    operation_id, mode = operations.pop()
    if mode.startswith("web-job:"):
        raise QueueJournalBlocked(
            "saved Web recovery must be resumed from its exact Web job"
        )

    loaded = load_queue_journal(operation_id)
    journal = loaded.journal
    if (
        loaded.status is not QueueLoadStatus.READY
        or journal is None
        or journal.operation_id != operation_id
        or journal.mode != mode
        or journal.retirements
        or len(journal.items) != len(recovery.items)
    ):
        raise QueueJournalBlocked(
            loaded.reason or "saved CLI recovery no longer matches its journal"
        )

    recovered_by_id = {item.item_id: item for item in recovery.items}
    if len(recovered_by_id) != len(recovery.items):
        raise QueueJournalBlocked("CLI recovery item identities are ambiguous")
    expected_actions = {
        QueuePhase.PENDING: StartupRecoveryAction.PENDING,
        QueuePhase.ACTIVE: StartupRecoveryAction.RESUME_DOWNLOAD,
        QueuePhase.RESOLVING: StartupRecoveryAction.FINALISE_COMPLETION,
        QueuePhase.COMPLETE: StartupRecoveryAction.FINALISE_COMPLETION,
    }
    recovery_bearing = []
    for queued in journal.items:
        recovered = recovered_by_id.get(queued.item_id)
        if (
            recovered is None
            or recovered.operation_id != operation_id
            or recovered.mode != mode
            or recovered.phase is not queued.phase
            or recovered.action is not expected_actions.get(queued.phase)
            or recovered.reason is not None
        ):
            raise QueueJournalBlocked(
                "saved CLI recovery changed before it could be resumed"
            )
        if queued.phase is not QueuePhase.PENDING:
            recovery_bearing.append(queued)

    if len(recovery_bearing) > 1:
        raise QueueJournalBlocked(
            "more than one active CLI queue item requires attention"
        )
    if recovery_bearing:
        queued = recovery_bearing[0]
        completion_input = parse_completion_input_record(
            queued.completion_input,
            expected_owner=RecoveryOwner(operation_id, queued.item_id),
        )
        planned_album = queued.planned.get("album")
        if (
            completion_input is None
            or completion_input.origin.kind is not CompletionOriginKind.CLI
            or completion_input.origin.reference != "download-queue"
            or not isinstance(planned_album, dict)
            or normalise_album_id(planned_album.get("id"))
            != normalise_album_id(completion_input.expectation.album_id)
        ):
            raise QueueJournalBlocked(
                "saved queue item does not belong to this CLI download"
            )

    ordered = list(journal.items)
    if recovery_bearing:
        ordered.remove(recovery_bearing[0])
        ordered.insert(0, recovery_bearing[0])
    try:
        items = [_deserialize_queue_item(item.planned) for item in ordered]
    except (KeyError, TypeError, ValueError) as exc:
        raise QueueJournalBlocked("saved CLI queue plan is invalid") from exc
    return loaded, items, mode, journal.saved_at, operation_id, bool(recovery_bearing)


def offer_resume_startup_recovery(args, token_source, recovery):
    """Offer the exact typed CLI operation named by startup recovery."""
    (
        loaded,
        items,
        mode,
        saved_at,
        operation_id,
        recovery_bearing,
    ) = _load_startup_recovery_queue(recovery)

    when = saved_at or "unknown time"
    label_for_mode = {
        "album_walk": "Album fill walk",
        "walk_queue": "Walk + queue",
    }.get(mode, mode)
    queue_path = loaded.paths[0] if len(loaded.paths) == 1 else cfg.QUEUE_JOURNAL_DIR

    print()
    heading = "Interrupted" if recovery_bearing else "Pending"
    log.info(fmt(
        C.BOLD + C.YELLOW,
        f"  ⚠  {heading} queue found: {len(items)} album(s) from {label_for_mode}",
    ))
    log.info(fmt(C.GRAY, f"     Saved: {when}"))
    log.info(fmt(C.GRAY, f"     File:  {queue_path}"))
    if recovery_bearing:
        log.info(fmt(C.GRAY,
            "     The exact interrupted album must finish recovery before "
            "other work."))
    else:
        log.info(fmt(C.GRAY,
            "     Resuming will download only what is in this saved queue."))
    print()

    while True:
        choices = "[Y]es / [k]eep for later"
        if not recovery_bearing:
            choices += " / [d]iscard"
        try:
            ans = input(fmt(C.CYAN, f"  Resume now? {choices}: ")).strip().lower()
        except EOFError:
            ans = ""
        if ans in ("", "y", "yes"):
            log.info(fmt(C.CYAN,
                f"\n  ⟳  Resuming {len(items)} saved album(s)…"))
            try:
                token = token_source() if callable(token_source) else token_source
                from qobuz_librarian.queue.executor import _execute_download_queue

                def _save_pending_progress():
                    save_pending_queue(items, mode=mode)

                _, drained = _execute_download_queue(
                    items,
                    args,
                    token,
                    on_progress=(
                        None if recovery_bearing else _save_pending_progress
                    ),
                    refresh_review=True,
                    consolidate_duplicates=mode != "album-now",
                )
                if getattr(args, "dry_run", False):
                    pass
                elif drained:
                    clear_queue_journal(operation_id)
                    log.info(fmt(
                        C.GREEN,
                        "  ✓ Resume complete; saved queue cleared.",
                    ))
                else:
                    settled = not recovery_bearing
                    if settled:
                        # The flush itself can park an album, so a queue that
                        # loaded purely pending may be recovery-bearing by now.
                        try:
                            save_pending_queue(items, mode=mode)
                        except QueueJournalBlocked:
                            settled = False
                    if settled:
                        log.info(fmt(C.YELLOW,
                            f"  ⚠  {len(items)} album(s) couldn't be "
                            "downloaded — saved queue kept for next launch."))
                    else:
                        log.info(fmt(C.YELLOW,
                            "  ⚠  The saved queue is not fully settled; its "
                            "durable recovery was kept for next launch."))
            except KeyboardInterrupt:
                log.info(fmt(C.YELLOW,
                    "\n  ⚠  Resume interrupted; saved queue kept for next launch."))
            except AuthLost:
                log.info(fmt(C.RED,
                    "  ✗ Auth lost during resume; saved queue kept for next launch."))
                raise
            except Exception as exc:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Resume failed: {exc}. Saved queue kept for next launch."))
            return False
        if ans in ("k", "keep"):
            log.info(fmt(C.GRAY,
                "  Keeping the saved queue. It'll prompt again next launch."))
            return False
        if not recovery_bearing and ans in ("d", "discard"):
            try:
                conf = input(fmt(C.YELLOW,
                    f"  Really discard {len(items)} queued album(s)? "
                    "Type DISCARD to confirm: ")).strip()
            except EOFError:
                conf = ""
            if conf == "DISCARD":
                clear_queue_journal(operation_id, explicit_discard=True)
                log.info(fmt(C.GRAY, "  Pending queue cleared."))
            else:
                log.info(fmt(C.GRAY, "  Discard cancelled."))
            return False
        allowed = "Y or k" if recovery_bearing else "Y, k, or d"
        log.info(fmt(C.GRAY, f"  Enter {allowed}."))


def offer_resume_pending_queue(args, token_source):
    """Startup hook. If a pending queue is present, prompt to resume / keep
    / discard. ``token_source`` may be a token or a lazy callable; the latter
    keeps local keep/discard choices reachable without download preflight."""
    loaded = load_pending_queue()
    items, mode, saved_at = loaded
    if not items:
        return False

    # Pretty-print the saved_at timestamp without dragging in tz juggling.
    when = saved_at or "unknown time"
    label_for_mode = {
        "album_walk": "Album fill walk",
        "walk_queue": "Walk + queue",
    }.get(mode or "", mode or "previous run")

    print()
    log.info(fmt(C.BOLD + C.YELLOW,
        f"  ⚠  Pending queue found: {len(items)} album(s) from {label_for_mode}"))
    log.info(fmt(C.GRAY, f"     Saved: {when}"))
    queue_path = loaded.paths[0] if len(loaded.paths) == 1 else cfg.QUEUE_JOURNAL_DIR
    log.info(fmt(C.GRAY, f"     File:  {queue_path}"))
    log.info(fmt(C.GRAY,
        "     This means a previous run accumulated download decisions but "
        "didn't"))
    log.info(fmt(C.GRAY,
        "     finish flushing them. Resuming will download only what's "
        "in the queue."))
    print()

    while True:
        try:
            ans = input(fmt(C.CYAN,
                "  Resume now? [Y]es / [k]eep for later / [d]iscard: "
            )).strip().lower()
        except EOFError:
            ans = ""
        if ans in ("", "y", "yes"):
            log.info(fmt(C.CYAN,
                f"\n  ⟳  Resuming flush of {len(items)} album(s)…"))
            try:
                token = token_source() if callable(token_source) else token_source
                # Lazy import to avoid a circular dependency at module load:
                # executor imports several names from this module.
                from qobuz_librarian.queue.executor import _execute_download_queue

                def _save_progress():
                    save_pending_queue(items, mode=mode)

                _, drained = _execute_download_queue(
                    items, args, token, on_progress=_save_progress,
                    refresh_review=True,
                    consolidate_duplicates=mode != "album-now",
                )
                if getattr(args, "dry_run", False):
                    pass  # preview only; leave the pending file untouched
                elif drained:
                    clear_pending_queue()
                    log.info(fmt(C.GREEN, "  ✓ Resume complete; saved queue cleared."))
                else:
                    try:
                        save_pending_queue(items, mode=mode)
                        log.info(fmt(C.YELLOW,
                            f"  ⚠  {len(items)} album(s) couldn't be "
                            "downloaded — saved queue kept for next launch."))
                    except QueueJournalBlocked:
                        # An album parked for attention during the flush; the
                        # journal holds it and must not be rewritten as pending.
                        log.info(fmt(C.YELLOW,
                            "  ⚠  The saved queue is not fully settled; its "
                            "durable recovery was kept for next launch."))
            except KeyboardInterrupt:
                log.info(fmt(C.YELLOW,
                    "\n  ⚠  Resume interrupted; saved queue kept for next launch."))
            except AuthLost:
                log.info(fmt(C.RED,
                    "  ✗ Auth lost during resume; saved queue kept for next launch."))
                raise
            except Exception as e:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Resume failed: {e}. Saved queue kept for next launch."))
            return False  # fall through to menu
        if ans in ("k", "keep"):
            log.info(fmt(C.GRAY, "  Keeping the pending queue. It'll prompt again next launch."))
            return False
        if ans in ("d", "discard"):
            try:
                conf = input(fmt(C.YELLOW,
                    f"  Really discard {len(items)} queued album(s)? "
                    "Type DISCARD to confirm: ")).strip()
            except EOFError:
                conf = ""
            if conf == "DISCARD":
                clear_pending_queue(explicit_discard=True)
                log.info(fmt(C.GRAY, "  Pending queue cleared."))
            else:
                log.info(fmt(C.GRAY, "  Discard cancelled."))
            return False
        log.info(fmt(C.GRAY, "  Enter Y, k, or d."))
