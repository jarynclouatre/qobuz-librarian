"""Staged-rip quality checks before local rewrite hooks run."""
from pathlib import Path

from qobuz_librarian.integrations.staging import (
    discard_group,
    discard_received_audio_trees,
    park_trees,
    restore_group,
)
from qobuz_librarian.library.catalog import read_album_dir
from qobuz_librarian.quality.decision import album_max_quality, track_targets
from qobuz_librarian.ui_cli.logging import log


def rip_shortfall(staged_dirs, qobuz_album, *, effective_tier=None, exempt=()):
    """Decide whether the staged rip came in below min(source, cap), each
    track against what Qobuz offers for it. ``exempt`` names the files that
    came at CD quality on purpose."""
    target = album_max_quality(qobuz_album, tier=effective_tier)
    staged = [t for d in staged_dirs for t in read_album_dir(d)
              if t.get("path") not in exempt]
    quals, n_unknown, n_below = [], 0, 0
    for t, (bits_wanted, rate_wanted) in zip(
            staged, track_targets(staged, qobuz_album, effective_tier)):
        bits = t.get("bits") or 0
        rate = t.get("sample_rate") or 0
        if not (bits and rate):
            n_unknown += 1
            continue
        quals.append((int(bits), int(rate)))
        if bits < bits_wanted or rate < rate_wanted:
            n_below += 1
    return {
        "under": n_below > 0 or n_unknown > 0,
        "n_below": n_below,
        "target": target,
        "worst": min(quals) if quals else None,
        "n_unknown": n_unknown,
    }


def quality_met_line(n_ok, cd_quality_tracks):
    """What the quality check says when the download meets the selected
    quality, or "" when every track came at CD quality, since the check
    leaves those tracks out."""
    if not cd_quality_tracks:
        return "Quality check: the download meets the selected quality."
    if n_ok > len(cd_quality_tracks):
        return "Quality check: the other tracks meet the selected quality."
    return ""


def verify_and_recover(qobuz_album, staged_dirs, *, effective_tier,
                       redownload_at_max=None, allow_retry=True, exempt=()):
    """Verify the staged rip and retry once at the max tier when safe.
    ``redownload_at_max`` returns the kept staged dirs and their exempt
    files."""
    sf = rip_shortfall(staged_dirs, qobuz_album, effective_tier=effective_tier,
                       exempt=exempt)
    recovered = retried = False
    if sf["under"] and effective_tier < 4 and allow_retry:
        retried = True
        fresh_dirs, exempt = redownload_at_max()
        if fresh_dirs:
            staged_dirs = fresh_dirs
            sf = rip_shortfall(staged_dirs, qobuz_album,
                               effective_tier=effective_tier, exempt=exempt)
            recovered = not sf["under"]
    return {
        "under": sf["under"],
        "recovered": recovered,
        "retried": retried,
        "n_below": sf["n_below"],
        "n_unknown": sf["n_unknown"],
        "served": sf["worst"],
        "target": sf["target"],
        "source": album_max_quality(qobuz_album, tier=4),
        "effective_tier": effective_tier,
        "staged_dirs": staged_dirs,
    }


def retry_preserves_track_coverage(original, retry):
    """True only when a retry preserves every known-good Qobuz track slot."""
    original_expected = original.get("_expected_track_keys")
    retry_expected = retry.get("_expected_track_keys")
    original_clean = original.get("_clean_track_keys")
    retry_clean = retry.get("_clean_track_keys")
    if not all(isinstance(value, list) for value in (
            original_expected, retry_expected, original_clean, retry_clean)):
        return False
    if (len(set(original_expected)) != len(original_expected)
            or len(set(retry_expected)) != len(retry_expected)
            or set(original_expected) != set(retry_expected)):
        return False
    return (
        retry.get("_exact_track_coverage") is True
        and not retry.get("_unmatched_audio")
        and bool(original_clean)
        and set(original_clean) <= set(retry_clean)
    )


def redownload_with_staged_fallback(staged_dirs, *, run_retry,
                                    collect_staged_dirs, collect_retry_files,
                                    retry_preserves_original):
    """Retry without losing the first usable staged rip if retry lands nothing
    or lands less of the album than the first rip did.

    Returns ``(staged_dirs, retry_kept)``.
    """
    originals = [Path(d) for d in staged_dirs if Path(d).exists()]
    if not originals:
        run_retry()
        return collect_staged_dirs(), True
    parked = park_trees(originals, "quality-retry", kind="quality")
    if parked is None:
        raise OSError("couldn't safely park the first staged rip for retry")

    def restore_first(fresh, retry_files):
        if fresh and not discard_received_audio_trees(fresh, retry_files):
            raise OSError(
                "higher-quality retry output changed; both staged states were "
                "kept for manual recovery")
        restored = restore_group(parked)
        if restored is None:
            raise OSError(
                "couldn't safely restore the first staged rip; both staged "
                "states were kept for manual recovery")
        return restored

    try:
        run_retry()
        fresh = collect_staged_dirs()
    except BaseException as exc:
        try:
            try:
                fresh = collect_staged_dirs()
            except OSError:
                # run_album_download retains its exact run before propagating
                # an abrupt failure, so its former public dirs are absent.
                fresh = []
            retry_files = collect_retry_files()
            restore_first(fresh, retry_files)
        except Exception as recovery_error:
            raise recovery_error from exc
        raise

    if fresh and retry_preserves_original():
        if not discard_group(parked):
            log.info(
                "  Higher-quality retry succeeded; the first rip changed "
                "while parked and was kept in the retry recovery folder.")
        return fresh, True
    if fresh:
        log.info("  Higher-quality retry did not preserve the first rip's "
                 "Qobuz track coverage; keeping the first rip.")
    retry_files = collect_retry_files()
    return restore_first(fresh, retry_files), False
