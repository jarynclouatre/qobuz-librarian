"""Interpret the unresolved tracks in a download result."""


def track_shortfall(result):
    """Return the failed, damaged, lossy-only and not-streamed track counts.

    Failed tracks can arrive on another try; a damaged one is a broken file
    Qobuz sent at the chosen quality, which another try may get again. Qobuz
    has lossy-only tracks only in a lossy format and does not stream the
    others at all."""
    n_broken = result.get("n_broken")
    if n_broken is None:
        # Results saved before damaged tracks were counted on their own.
        n_broken = min(len(result.get("broken_tracks") or []),
                       result.get("n_lossy", 0))
    n_lossy_only = result.get("n_lossy_only")
    if n_lossy_only is None:
        n_lossy_only = max(result.get("n_lossy", 0) - n_broken, 0)
    n_unavailable = result.get("n_unavailable", 0)
    return (result.get("n_fail", 0) - n_unavailable, n_broken, n_lossy_only,
            n_unavailable)


def incomplete_track_counts(result):
    """Return how many missing tracks another try could fetch, and how many
    need another source."""
    n_failed, n_broken, n_lossy_only, n_unavailable = track_shortfall(result)
    return n_failed + n_broken, n_lossy_only + n_unavailable


def shortfall_fields(result):
    """The missing-track fields a finished download reports, and the tracks
    that came at CD quality because Qobuz sent a broken file."""
    _, n_broken, n_lossy_only, n_unavailable = track_shortfall(result)
    return {
        "n_broken": n_broken,
        "n_lossy_only": n_lossy_only,
        "n_unavailable": n_unavailable,
        "unavailable_tracks": result.get("unavailable_tracks", []),
        "cd_quality_tracks": result.get("cd_quality_tracks", []),
    }


def processing_attention(result):
    """Whether the work after the download left something to look at."""
    return bool(
        result.get("downsample_errors", 0)
        or result.get("downsample_flush_warnings", 0)
        or result.get("downsample_cancelled")
        or result.get("consolidation_interrupted")
        or result.get("siblings_preserved")
    )


_CD_QUALITY_OPENING = "Qobuz sent a broken file at the chosen quality for "


def cd_quality_note(tracks):
    """Say which tracks came at CD quality, and why."""
    return (f"{_CD_QUALITY_OPENING}{', '.join(tracks)}, so "
            f"{'it' if len(tracks) == 1 else 'they'} came at CD quality.")


_SIBLINGS_KEPT_OPENING = "Kept your other "


def siblings_kept_note(names):
    """Say which other folders of the album a Gap Fill kept, and why."""
    one = len(names) == 1
    return (f"{_SIBLINGS_KEPT_OPENING}{'copy' if one else 'copies'} "
            f"{', '.join(names)}, since not every track in "
            f"{'it' if one else 'them'} matches one here.")


def download_notes_in(summary):
    """The notes a download's summary ends with, or ""."""
    summary = summary or ""
    starts = [start for start in (summary.find(_SIBLINGS_KEPT_OPENING),
                                  summary.find(_CD_QUALITY_OPENING))
              if start >= 0]
    return summary[min(starts):] if starts else ""


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
    if processing_attention(result):
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
    retryable, _ = incomplete_track_counts(result)
    _, _, lossy_only, unavailable = track_shortfall(result)
    if unavailable and not retryable:
        return "Qobuz does not stream the missing tracks"
    if lossy_only and not retryable:
        return "Qobuz only had lossy files"
    if result.get("n_broken") and not result.get("n_fail"):
        return "Qobuz sent broken files"
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
        if processing_attention(result):
            return "failed", "processing"
        return "done", kind
    if kind == "backup" or (result.get("result") == "partial" and result.get("imported")):
        return "failed", kind
    benign = {"already_complete", "skipped_already_higher_quality",
              "skipped_has_extras", "dry_run", "user_skipped",
              "lossy_only", "no_tracks", "cancelled"}
    if result.get("result") not in benign and not result.get("imported"):
        # No later try fetches what Qobuz cannot supply, so it is not retried.
        return "failed", "lossy" if kind == "lossy" else ""
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
