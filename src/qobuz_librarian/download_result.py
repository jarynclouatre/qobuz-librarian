"""Interpret the unresolved tracks in a download result."""


def incomplete_track_counts(result):
    """Return retryable and lossy-only missing-track counts."""
    n_failed = result.get("n_fail", 0)
    n_rejected = result.get("n_lossy", 0)
    n_broken = result.get("n_broken")
    if n_broken is None:
        n_broken = min(len(result.get("broken_tracks") or []), n_rejected)
    n_lossy_only = result.get("n_lossy_only")
    if n_lossy_only is None:
        n_lossy_only = max(n_rejected - n_broken, 0)
    return n_failed + n_broken, n_lossy_only
