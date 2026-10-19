"""What a finished or failed download says about itself."""
import asyncio

from qobuz_librarian import disk_support, download_result
from qobuz_librarian.api import auth as api_auth
from qobuz_librarian.api.auth import (
    AuthLost,
    CredentialChanged,
    DownloaderNotReady,
    DownloadsRefused,
    QobuzEntitlementError,
    QobuzError,
    QobuzUnavailable,
)
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.web import jobs as job_mgr
from qobuz_librarian.web import storage


def _download_error_message(exc, fallback: str) -> str:
    """Give download preparation failures the same Web-facing diagnosis."""
    if isinstance(exc, asyncio.TimeoutError):
        return "Timed out reaching the Qobuz API. Try again."
    if isinstance(exc, QobuzUnavailable):
        return str(exc)
    if isinstance(exc, DownloadsRefused):
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
        "Left as it is: the album's files changed after it was queued, or "
        "could not be read. A run that stopped part way may already have "
        "added tracks. Check the album again to see what it still needs."
    ),
    "replacement_aborted_catalogue_failed": (
        "The album's Beets entries could not be read, so the replacement "
        "was not made. Your files are unchanged."
    ),
    "not_imported": "Downloaded, but the import didn't land. Library unchanged.",
}
_UPGRADE_REFUSED = (
    "The new copy couldn't be verified against yours, so your original was "
    "kept and the download was discarded."
)


def _summarize_download_result(r):
    """One-line job summary from process_album's result dict, naming any
    other folder of the album it kept and any track that came at CD
    quality."""
    summary = _download_summary(r)
    if summary and (r or {}).get("imported"):
        if r.get("siblings_kept"):
            summary += f" {download_result.siblings_kept_note(r['siblings_kept'])}"
        if r.get("cd_quality_tracks"):
            summary += f" {download_result.cd_quality_note(r['cd_quality_tracks'])}"
    return summary


def _download_summary(r):
    """The job summary without the CD-quality note.

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
                parts.append(
                    "highest-source retry remained below target quality"
                    if verdict.get("retried") else "below target quality")
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
    if kind == "already_complete" and r.get("unavailable_tracks"):
        n = len(r["unavailable_tracks"])
        return (f"Every track Qobuz streams is in the library; "
                f"{plural(n, 'track')} {'is' if n == 1 else 'are'} not "
                "available on Qobuz.")
    if kind == "incomplete" and r.get("upgrade_put_back"):
        return _UPGRADE_REFUSED
    if kind == "disk_refused":
        return " ".join(filter(None, (
            disk_support.refusal("whole_albums"), "Nothing was downloaded.")))
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


def beyond_retry(lossy_only, unavailable):
    """Say which missing tracks no retry can fetch, or ""."""
    parts = []
    if lossy_only:
        parts.append(
            f"{plural(lossy_only, 'track')} "
            f"{'is' if lossy_only == 1 else 'are'} only available lossy on Qobuz")
    if unavailable:
        parts.append(
            f"{plural(unavailable, 'track')} "
            f"{'is' if unavailable == 1 else 'are'} not available on Qobuz")
    return " and ".join(parts)


def _undeliverable_album_error(r):
    """Say how short the album came, and what the user can do about it."""
    landed = r.get("n_ok", 0)
    short = (
        r.get("n_fail", 0) + r.get("n_broken", 0) + r.get("n_lossy_only", 0)
    )
    if landed and short:
        opening = f"Qobuz delivered {landed} of {plural(landed + short, 'track')}"
    else:
        opening = "Qobuz couldn't deliver every track"
    text = (f"{opening}, so nothing was added to your library and the part "
            "that downloaded was discarded.")
    if r.get("n_broken"):
        text += (" Qobuz sent a broken file at this quality for "
                 f"{plural(r['n_broken'], 'track')}.")
        if not r.get("n_fail"):
            return text
    return (f"{text} Try again later; if it stops at the same track every "
            "time, Qobuz can't serve that track.")


def download_error(result, *, job_controls=True):
    """The error line a finished download shows, or None when it has none.

    Without ``job_controls``, as on a terminal row with no log or Retry, it
    leaves out the hint pointing at them."""

    def hint(text):
        return f" {text}" if job_controls else ""

    retryable, _ = download_result.incomplete_track_counts(result)
    _, _, lossy_only, unavailable = download_result.track_shortfall(result)
    status, kind = download_result.download_job_outcome(result)
    if kind == "backup":
        if result.get("catalogue_unverified"):
            return (
                "The album's Beets catalogue entries could not be reconciled "
                "safely. A backup was retained. Review it under Settings > "
                "Diagnostics before downloading this album again."
            )
        if result.get("recovery_unverified"):
            return (
                "The album recovery could not be verified complete. A backup "
                "was retained. Review it under Settings > Diagnostics before "
                "downloading this album again."
            )
        return (
            "The replacement could not be verified complete. Your "
            "original was retained as a backup. Review it under Settings "
            "> Diagnostics before downloading this album again."
        )
    if kind == "quality":
        if (result.get("quality_verdict") or {}).get("retried"):
            return (
                "The album downloaded, but it still finished below the target "
                "quality after the automatic retry."
            )
        return "The album downloaded, but it finished below the target quality."
    if kind == "processing":
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
        return (
            f"The album imported, but {detail}."
            + hint("Check the job log before retrying.")
        )
    if kind == "lossy":
        kept = (" Your original album was kept."
                if result.get("upgrade_put_back") else "")
        return (
            f"{beyond_retry(lossy_only, unavailable)}. The album is "
            "incomplete and needs another source." + kept
        )
    if kind:
        if not retryable:
            return (
                "The album imported, but the download reported unfinished "
                "work." + hint("Check the job log before retrying.")
            )
        damaged = result.get("n_broken", 0)
        not_arrived = retryable - damaged
        causes = []
        if damaged:
            causes.append(
                "Qobuz sent a broken file at this quality for "
                f"{plural(damaged, 'track')}")
        if not_arrived:
            causes.append(f"{plural(not_arrived, 'track')} did not arrive")
        error = f"{' and '.join(causes)}."
        if not_arrived:
            error += hint(f"Retry fetches {'it' if not_arrived == 1 else 'them'}.")
        if lossy_only or unavailable:
            error += f" {beyond_retry(lossy_only, unavailable)}."
        return error
    if status == "failed":
        if result.get("rate_limited"):
            return "Qobuz rate-limited this download. Try again later."
        if result.get("companions_unsettled"):
            return (
                "The new copy is in your library, but files from the album "
                "it replaced could not be carried into it. "
                + ("That album is kept whole in a backup in Settings > "
                   "Diagnostics." if result.get("companions_settled") else
                   "The next time the app starts, it keeps that album as a "
                   "backup in Settings > Diagnostics.")
            )
        if result.get("result") == "incomplete":
            if result.get("upgrade_put_back"):
                return _UPGRADE_REFUSED
            return _undeliverable_album_error(result)
        n_failed = result.get("n_fail", 0) - unavailable
        other = beyond_retry(lossy_only, unavailable)
        other = f" {other}." if other else ""
        if n_failed:
            return (f"{plural(n_failed, 'track')} failed."
                    + hint("See job log.") + other)
        if result.get("import_denied"):
            folder, _is_host = storage._resolve_host_path(
                result["import_denied"])
            return (f"Downloaded, but Beets was not allowed to write in "
                    f"{folder}. Check that folder's permissions, then retry.")
        if result.get("n_ok"):
            return "Downloaded, but the import failed." + hint("See job log.")
        if retryable:
            return (
                f"{plural(retryable, 'track')} did not arrive as a "
                "complete file, so nothing was added to your library."
                + hint("The job log names them.") + other)
        if result.get("result") in _DOWNLOAD_SUMMARY_LABELS:
            return _DOWNLOAD_SUMMARY_LABELS[result["result"]]
        return "No tracks were retrieved." + hint("The job log says why.")
    if result.get("imported") and result.get("n_fail", 0) > 0:
        return (f"{plural(result['n_fail'], 'track')} failed."
                + hint("See job log."))
    return None


def _mark_download_attention(job, result):
    """Record what a finished download left that still needs attention."""
    status, kind = download_result.download_job_outcome(result)
    job.status = job_mgr.JobStatus(status)
    job.tracks_missing = download_result.tracks_missing(result)
    job.error = download_error(result)
    if kind == "backup":
        job.attention = "backup"
        if not isinstance(job.execute_args, dict):
            job.execute_args = {}
        job.execute_args["retry_disabled"] = "backup"
    elif kind == "quality":
        job_mgr.record_quality_shortfall(job, result.get("quality_verdict"))
    elif kind == "processing":
        job.attention = "processing"
    elif kind == "lossy":
        job.attention = "lossy"
        job.execute_args["retry_disabled"] = "lossy"
    else:
        job.attention = "partial"
