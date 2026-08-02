"""Deferring Ctrl-C across one raw-resource handoff.

A KeyboardInterrupt that lands between acquiring a descriptor and recording
its owner leaks the resource or strands its cleanup. Callers wrap exactly
that window: the interrupt is parked, the handoff finishes, and the signal
is re-delivered the moment the window closes. Worker threads never receive
SIGINT, so off the main thread the callback just runs.
"""
import signal
import threading


def run_sigint_deferred(callback, *, unavailable=OSError,
                        detail="interrupt-safe access is unavailable"):
    """Run ``callback`` with SIGINT parked, re-delivering it afterwards.

    When the deferring handler can't be installed at all, raises
    ``unavailable(detail)`` — callers pass their own error type so the
    failure surfaces in their subsystem's vocabulary.
    """
    if threading.current_thread() is not threading.main_thread():
        return callback()
    pending = False
    interrupted_frame = None

    def defer_sigint(_signum, frame):
        nonlocal pending, interrupted_frame
        pending = True
        interrupted_frame = frame

    try:
        previous_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, defer_sigint)
    except (OSError, RuntimeError, ValueError) as exc:
        raise unavailable(detail) from exc
    try:
        return callback()
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        if pending and previous_handler is not signal.SIG_IGN:
            if callable(previous_handler):
                previous_handler(signal.SIGINT, interrupted_frame)
            else:
                signal.raise_signal(signal.SIGINT)
