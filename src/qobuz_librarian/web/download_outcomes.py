"""What a finished or failed download says about itself."""
import asyncio

from qobuz_librarian import download, download_result
from qobuz_librarian.api import auth as api_auth
from qobuz_librarian.api.auth import (
    AuthLost,
    CredentialChanged,
    DownloaderNotReady,
    QobuzEntitlementError,
    QobuzError,
    QobuzUnavailable,
)
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.web import jobs as job_mgr


def _download_error_message(exc, fallback: str) -> str:
    """Give download preparation failures the same Web-facing diagnosis."""
    if isinstance(exc, asyncio.TimeoutError):
        return "Timed out reaching the Qobuz API. Try again."
    if isinstance(exc, QobuzUnavailable):
        return str(exc)
    if isinstance(exc, AuthLost):
        return "Qobuz rejected the saved token. Reconnect in Settings."
    if isinstance(exc, DownloaderNotReady):
        return (
            "Your Qobuz token works, but downloads also need your Qobuz "
            "user ID. Add it in Settings."
        )
    if isinstance(exc, CredentialChanged):
        return "Qobuz credentials changed while this was starting. Try again."
    if isinstance(exc, QobuzEntitlementError):
        return "Your Qobuz account cannot perform this download."
    if isinstance(exc, QobuzError):
        if api_auth.friendly_qobuz_error(exc).startswith("HTTP 404"):
            return "No album with that id. Check the URL or use Search."
        return "Qobuz answered with an error. Try again."
    return fallback


_DOWNLOAD_SUMMARY_LABELS = {
    "already_complete": "Album already complete. Nothing to download.",
    "skipped_already_higher_quality": "Skipped: the library already has higher quality.",
    "skipped_has_extras": "Skipped: the library copy includes extra tracks.",
    "upgrade_only_no_op": "Already at or above the target quality.",
    "upgrade_no_local_tracks": "This album isn't in your library any more.",
    "dry_run": "Dry run. Nothing downloaded.",
    "user_skipped": "Skipped at confirmation.",
    "lossy_only": "Qobuz only had lossy versions. Nothing downloaded.",
    "no_tracks": "Qobuz returned no tracks for this album.",
    "cancelled": "Cancelled. Nothing was imported.",
    "incomplete": "Qobuz couldn't deliver the whole album. Nothing was imported.",
    "upgrade_aborted_backup_failed": "Upgrade aborted: couldn't back up the original.",
    "stale_candidate": (
        "The album's local files changed or could not be read before the "
        "download started, so nothing was downloaded. Try again."
    ),
    "replacement_aborted_catalogue_failed": (
        "The album's Beets entries could not be read, so the replacement "
        "was not made. Your files are unchanged."
    ),
    "not_imported": "Downloaded, but the import didn't land. Library unchanged.",
}


def _summarize_download_result(r):
    """One-line job summary from process_album's result dict.

    Picks a phrase per result kind for the documented non-success branches,
    or builds the "N tracks downloaded" tally for an actual rip. Returns
    "" if there's nothing useful to say (process_album returned None / {})."""
    if not r:
        return ""
    kind = r.get("result")
    if kind == "cancelled" and (
        r.get("catalogue_unverified")
        or r.get("recovery_unverified")
        or r.get("upgrade_unverified")
    ):
        return "Cancelled. Nothing was imported. A safety backup was retained."
    if kind == "partial":
        landed = plural(r.get("n_ok", 0), "track")
        if not r.get("imported"):
            summary = (
                f"{landed} downloaded, but the incomplete album was not "
                "imported."
            )
            if (
                r.get("catalogue_unverified")
                or r.get("recovery_unverified")
                or r.get("upgrade_unverified")
            ):
                summary += " A safety backup was retained for review."
            return summary
        parts = [f"{landed} downloaded"]
        if r.get("catalogue_unverified"):
            parts.append("Beets catalogue needs attention; backup retained")
        elif r.get("recovery_unverified"):
            parts.append("recovery could not be verified; backup retained")
        elif r.get("upgrade_unverified"):
            parts.append("upgrade could not be verified; original backup retained")
        else:
            verdict = r.get("quality_verdict") or {}
            if verdict.get("under") and not verdict.get("recovered"):
                parts.append("highest-source retry remained below target quality")
            if r.get("downsample_errors"):
                parts.append(
                    f"{plural(r['downsample_errors'], 'file')} could not be "
                    "downsampled"
                )
            if r.get("downsample_flush_warnings"):
                parts.append(
                    f"{plural(r['downsample_flush_warnings'], 'rewritten file')} "
                    "could not be confirmed flushed"
                )
            if r.get("downsample_cancelled"):
                parts.append("post-download downsample stopped early")
            if r.get("consolidation_interrupted"):
                parts.append("duplicate cleanup stopped early")
            retryable, lossy_only = download_result.incomplete_track_counts(r)
            if r.get("siblings_preserved") and not (retryable or lossy_only):
                parts.append("sibling cleanup needs review")
        return ", ".join(parts) + "."
    if kind in _DOWNLOAD_SUMMARY_LABELS:
        return _DOWNLOAD_SUMMARY_LABELS[kind]
    if not r.get("imported"):
        return ""
    n_ok = r.get("n_ok", 0)
    n_fail = r.get("n_fail", 0)
    n_lossy = r.get("n_lossy", 0)
    parts = [f"{plural(n_ok, 'track')} downloaded"]
    if n_fail:
        parts.append(f"{n_fail} failed")
    if n_lossy:
        parts.append(f"{n_lossy} lossy-dropped")
    if r.get("catalogue_unverified"):
        parts.append("Beets catalogue needs attention; backup retained")
    elif r.get("recovery_unverified"):
        parts.append("recovery backup retained for review")
    elif r.get("upgrade_unverified"):
        parts.append("upgrade couldn't be verified; original kept")
    elif r.get("auto_upgrade"):
        parts.append("auto-upgrade verified")
    return ", ".join(parts) + "."


def _undeliverable_album_error(r, album=None):
    """Say how short the album came, and what the user can do about it."""
    if album is not None and download.disc_names_overwrite(album):
        return (
            "Tracks that share a number and title on different discs "
            "overwrote each other because disc_subdirectories is off in the "
            "streamrip config, so nothing was added to your library. Turn it "
            "on and try again."
        )
    landed = r.get("n_ok", 0)
    short = (
        r.get("n_fail", 0) + r.get("n_broken", 0) + r.get("n_lossy_only", 0)
    )
    if landed and short:
        opening = f"Qobuz delivered {landed} of {plural(landed + short, 'track')}"
    else:
        opening = "Qobuz couldn't deliver every track"
    return (
        f"{opening}, so nothing was added to your library and the part that "
        "downloaded was discarded. Try again later; if it stops at the same "
        "track every time, Qobuz can't serve that track."
    )


def _mark_download_attention(job, result):
    """Mark a job failed when download details still need attention."""
    retryable, lossy_only = download_result.incomplete_track_counts(result)
    status, kind = download_result.download_job_outcome(result)
    job.status = job_mgr.JobStatus(status)
    if kind == "backup":
        job.attention = "backup"
        if not isinstance(job.execute_args, dict):
            job.execute_args = {}
        job.execute_args["retry_disabled"] = "backup"
        if result.get("catalogue_unverified"):
            job.error = (
                "The album's Beets catalogue entries could not be reconciled "
                "safely. A backup was retained. Review it under Settings > "
                "Diagnostics before downloading this album again."
            )
        elif result.get("recovery_unverified"):
            job.error = (
                "The album recovery could not be verified complete. A backup "
                "was retained. Review it under Settings > Diagnostics before "
                "downloading this album again."
            )
        else:
            job.error = (
                "The replacement could not be verified complete. Your "
                "original was retained as a backup. Review it under Settings "
                "> Diagnostics before downloading this album again."
            )
        return
    if kind == "quality":
        job_mgr.record_quality_shortfall(job, result.get("quality_verdict"))
        job.error = (
            "The album downloaded, but it still finished below the target "
            "quality after the automatic retry."
        )
        return
    if kind == "processing":
        job.attention = "processing"
        messages = []
        if result.get("downsample_errors"):
            messages.append(
                f"{plural(result['downsample_errors'], 'file')} could not be "
                "downsampled"
            )
        if result.get("downsample_flush_warnings"):
            messages.append(
                f"{plural(result['downsample_flush_warnings'], 'rewritten file')} "
                "could not be confirmed flushed to disk"
            )
        if result.get("downsample_cancelled"):
            messages.append("post-download downsampling stopped early")
        if result.get("consolidation_interrupted"):
            messages.append("duplicate cleanup stopped early")
        if result.get("siblings_preserved"):
            messages.append("sibling cleanup needs review")
        detail = "; ".join(messages) or "post-download work did not finish"
        job.error = (
            f"The album imported, but {detail}. Check the job log before "
            "retrying."
        )
        return
    if kind == "lossy":
        job.attention = "lossy"
        job.execute_args["retry_disabled"] = "lossy"
        job.error = (
            f"{plural(lossy_only, 'track')} "
            f"{'is' if lossy_only == 1 else 'are'} only available "
            "lossy on Qobuz. The album is incomplete and needs another "
            "source."
        )
        return
    job.attention = "partial"
    if retryable:
        job.error = (
            f"{plural(retryable, 'track')} "
            f"{'is' if retryable == 1 else 'are'} still missing. "
            f"Retry fetches {'it' if retryable == 1 else 'them'}."
        )
        if lossy_only:
            job.error += (
                f" {plural(lossy_only, 'track')} can only be found "
                "lossy on Qobuz and needs another source."
            )
    else:
        job.error = (
            "The album imported, but the download reported unfinished work. "
            "Check the job log before retrying."
        )
