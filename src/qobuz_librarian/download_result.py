"""Interpret the unresolved tracks in a download result."""


def incomplete_track_counts(result):
    """Return retryable and lossy-only missing-track counts."""
    n_broken = result.get("n_broken")
    if n_broken is None:
        # Results saved before damaged tracks were counted on their own.
        n_broken = min(len(result.get("broken_tracks") or []),
                       result.get("n_lossy", 0))
    n_lossy_only = result.get("n_lossy_only")
    if n_lossy_only is None:
        n_lossy_only = max(result.get("n_lossy", 0) - n_broken, 0)
    return result.get("n_fail", 0) + n_broken, n_lossy_only


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


def failure_cause(result):
    """Why a download that added nothing failed, as a short clause."""
    if result.get("rate_limited"):
        return "Qobuz rate-limited the download"
    if result.get("result") in ("incomplete", "partial"):
        return "Qobuz couldn't deliver every track"
    if result.get("n_ok"):
        return "the import failed"
    retryable, lossy_only = incomplete_track_counts(result)
    if lossy_only and not retryable:
        return "Qobuz only had lossy files"
    if result.get("n_broken") and not result.get("n_fail"):
        return "the tracks arrived damaged"
    if retryable:
        return "no track arrived"
    return ""


def tracks_missing(result):
    """How many tracks an album that imported still lacks."""
    if (
        result.get("result") != "partial"
        or not result.get("imported")
        or download_attention_kind(result) not in ("partial", "lossy")
    ):
        return 0
    return sum(incomplete_track_counts(result))


def download_job_outcome(result):
    """The status ("done" or "failed") and attention a download earns."""
    kind = download_attention_kind(result)
    if tracks_missing(result):
        return "done", kind
    if kind == "backup" or (result.get("result") == "partial" and result.get("imported")):
        return "failed", kind
    benign = {"already_complete", "skipped_already_higher_quality",
              "skipped_has_extras", "dry_run", "user_skipped",
              "lossy_only", "no_tracks", "cancelled"}
    if result.get("result") not in benign and not result.get("imported"):
        return "failed", ""
    return "done", ""


def _quality_pair(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        bits, rate = int(value[0]), int(value[1])
    except (TypeError, ValueError, OverflowError):
        return None
    return [bits, rate] if bits > 0 and rate > 0 else None


def quality_shortfall_record(verdict):
    if not isinstance(verdict, dict):
        return {}
    target = _quality_pair(verdict.get("target"))
    if target is None:
        return {}
    record = {
        "version": 1,
        "target": target,
        "served": _quality_pair(verdict.get("served")),
        "source": _quality_pair(verdict.get("source")),
        "n_below": max(0, int(verdict.get("n_below") or 0)),
        "n_unknown": max(0, int(verdict.get("n_unknown") or 0)),
        "retried": verdict.get("retried") is True,
        "recovered": verdict.get("recovered") is True,
    }
    tier = verdict.get("effective_tier")
    if type(tier) is int and 2 <= tier <= 4:
        record["effective_tier"] = tier
    return record
