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


def download_attention_kind(result):
    """Classify unfinished download work for status and exit handling."""
    if not isinstance(result, dict):
        return ""
    if (
        result.get("upgrade_unverified")
        or result.get("catalogue_unverified")
        or result.get("recovery_unverified")
    ):
        return "backup"
    retryable, lossy_only = incomplete_track_counts(result)
    if retryable:
        return "partial"
    if lossy_only:
        return "lossy"
    verdict = result.get("quality_verdict") or {}
    if verdict.get("under") and not verdict.get("recovered"):
        return "quality"
    if (
        result.get("downsample_errors", 0)
        or result.get("downsample_flush_warnings", 0)
        or result.get("downsample_cancelled")
        or result.get("consolidation_interrupted")
        or result.get("siblings_preserved")
    ):
        return "processing"
    if result.get("result") == "partial":
        return "partial"
    return ""
