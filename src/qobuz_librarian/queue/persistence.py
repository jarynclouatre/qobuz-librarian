"""The terminal's resume prompt for a saved queue, and saving a run's queue."""

from datetime import datetime

from qobuz_librarian import completion
from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost
from qobuz_librarian.completion import CompletionOriginKind, RecoveryOwner
from qobuz_librarian.queue.journal import (
    QueueJournalBlocked,
    QueueLoadStatus,
    QueuePhase,
    _deserialize_queue_item,
    clear_queue_journal,
    load_queue_journal,
    save_pending_queue,
)
from qobuz_librarian.ui_cli.ask import ask
from qobuz_librarian.ui_cli.colors import C, fmt, truncate, wrap
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import log, vlog


def _queue_heading(kind, items, mode, saved_at):
    """The line that opens a resume prompt: what the queue came from, how
    many albums it holds and when it was saved, in local time."""
    source = {
        "album_walk": "a Gap Fill walk",
        "walk_queue": "a Library walk",
        "album-now": "an album search",
        "artist": "Artist mode",
    }.get(mode or "", "an earlier run")
    when = (datetime.fromisoformat(saved_at).astimezone().strftime(
        "%Y-%m-%d %H:%M") if saved_at else "at an unknown time")
    return fmt(C.BOLD + C.YELLOW, wrap(
        f"{kind} download queue from {source}: "
        f"{plural(len(items), 'album')}, saved {when}.",
        indent="  ⚠  ", hanging="     "))


def queue_saver(mode, *, dry_run=False):
    """Persist a run's queue, tolerating a journal that recovery holds.

    A durable item that stops for attention stays blocked in the journal on
    purpose, and while that stands the journal refuses to be rewritten as
    pending. The run can't settle it; that happens at the next launch, so it
    says so once and carries on rather than dying on the refusal. A dry run
    saves nothing.
    """
    told = []

    def save(items):
        if dry_run:
            return
        try:
            save_pending_queue(items, mode=mode)
        except QueueJournalBlocked:
            if not told:
                told.append(True)
                log.info(fmt(C.YELLOW,
                    "  ⚠  An album is parked for recovery, so the saved queue "
                    "is being kept exactly as it stands until the next "
                    "launch."))

    return save


def kept_queue_note(queue):
    """Where a run's unfinished queue waits, and how to run it."""
    return fmt(C.GRAY, wrap(
        f"{plural(len(queue), 'album')} kept in the saved queue in "
        f"{cfg.QUEUE_JOURNAL_DIR}. Run Qobuz Librarian with no mode or search "
        "argument to resume it.",
        indent="  ", hanging="  "))


# Albums a resume prompt names before it counts the rest.
_QUEUE_ALBUMS_SHOWN = 5


def _queued_album_lines(items):
    """The albums a saved queue holds, a few by name and a count of the
    rest."""
    lines = []
    for item in items[:_QUEUE_ALBUMS_SHOWN]:
        album = item.get("album") or {}
        artist = (album.get("artist") or {}).get("name") or "?"
        title = album.get("title") or item.get("label") or "?"
        version = album.get("version") or ""
        if version and version.lower() not in title.lower():
            title = f"{title} ({version})"
        lines.append(fmt(C.WHITE, f"     • {truncate(f'{artist} - {title}', 70)}"))
    if len(items) > _QUEUE_ALBUMS_SHOWN:
        lines.append(fmt(C.GRAY,
            f"     … and {len(items) - _QUEUE_ALBUMS_SHOWN} more"))
    return "\n".join(lines)


def _load_startup_recovery_queue(recovery):
    """Bind one typed CLI recovery result back to its exact journal."""
    # cli.py imports this module at the top, so a startup-recovery import here
    # would reach the web job store and its dependencies on every command.
    # Only a resume needs them.
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
        completion_input = completion.parse_completion_input_record(
            queued.completion_input,
            expected_owner=RecoveryOwner(operation_id, queued.item_id),
        )
        planned_album = queued.planned.get("album")
        if (
            completion_input is None
            or completion_input.origin.kind is not CompletionOriginKind.CLI
            or completion_input.origin.reference != "download-queue"
            or not isinstance(planned_album, dict)
            or completion.normalise_album_id(planned_album.get("id"))
            != completion.normalise_album_id(completion_input.expectation.album_id)
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

    queue_path = loaded.paths[0] if len(loaded.paths) == 1 else cfg.QUEUE_JOURNAL_DIR

    print()
    log.info(_queue_heading(
        "Interrupted" if recovery_bearing else "Saved",
        items, mode, saved_at))
    log.info(_queued_album_lines(items))
    vlog(f"resume queue file: {queue_path}")
    if recovery_bearing:
        log.info(fmt(C.GRAY,
            "     The interrupted album has to finish before other work."))
    else:
        log.info(fmt(C.GRAY,
            "     Resuming downloads only the albums in it."))
    print()

    while True:
        choices = "[Y]es / [k]eep for later"
        if not recovery_bearing:
            choices += " / [d]iscard"
        ans = ask(f"  Resume now? {choices}: ")
        if ans is None:
            return None
        if ans in ("", "y", "yes"):
            log.info(fmt(C.CYAN,
                f"\n  ⟳  Resuming {plural(len(items), 'saved album')}…"))
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
                            f"  ⚠  {plural(len(items), 'album')} couldn't be "
                            "downloaded - saved queue kept for next launch."))
                    else:
                        log.info(fmt(C.YELLOW,
                            "  ⚠  The saved queue is not fully settled; its "
                            "durable recovery was kept for next launch."))
            except KeyboardInterrupt:
                log.info(fmt(C.YELLOW,
                    "\n  ⚠  Resume interrupted; saved queue kept for next launch."))
            except AuthLost:
                log.info(fmt(C.RED,
                    "  ✗ Qobuz rejected the token during the resume; the "
                    "saved queue was kept for the next launch."))
                raise
            except Exception as exc:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Resume failed: {exc}. Saved queue kept for next launch."))
            return "resume"
        if ans in ("k", "keep"):
            log.info(fmt(C.GRAY,
                "  Keeping the saved queue."))
            return "keep"
        if not recovery_bearing and ans in ("d", "discard"):
            conf = ask(
                f"  Really discard {plural(len(items), 'queued album')}? "
                "Type DISCARD to confirm: ",
                colour=C.YELLOW, lower=False,
            )
            if conf is None:
                return None
            if conf == "DISCARD":
                clear_queue_journal(operation_id, explicit_discard=True)
                log.info(fmt(C.GRAY, "  Pending queue cleared."))
                return "discard"
            else:
                log.info(fmt(C.GRAY, "  Discard cancelled."))
            return "keep"
        allowed = "Y or k" if recovery_bearing else "Y, k, or d"
        log.info(fmt(C.GRAY, f"  Enter {allowed}."))

