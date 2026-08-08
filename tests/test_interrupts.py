import signal
import threading

import pytest

from qobuz_librarian import interrupts


def test_pending_sigint_is_delivered_after_the_handoff_finishes():
    events = []

    def prior_handler(_signum, _frame):
        events.append("delivered")

    previous = signal.signal(signal.SIGINT, prior_handler)
    try:
        def handoff():
            events.append("started")
            signal.raise_signal(signal.SIGINT)
            events.append("finished")
            return "adopted"

        assert interrupts.run_sigint_deferred(handoff) == "adopted"
    finally:
        signal.signal(signal.SIGINT, previous)

    assert events == ["started", "finished", "delivered"]
