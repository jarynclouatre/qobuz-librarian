"""Templates and what every page render shares: context, notices and error pages."""
import hashlib
import math
import os
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from qobuz_librarian import __version__
from qobuz_librarian import config as cfg
from qobuz_librarian.library import generation_state
from qobuz_librarian.web import auth as web_auth
from qobuz_librarian.web import (
    job_labels,
    job_persistence,
    new_release_checks,
    qobuz_access,
    queue_recovery,
    review_badges,
    runtime,
    settings_store,
    write_gate,
)
from qobuz_librarian.web import jobs as job_mgr


def _ql_notice_html(kind: str, body: str) -> str:
    # The copy sits in one span: the notice box is a flex row, and bare text
    # beside a link would be spaced apart as separate flex items.
    return (
        f'<div class="ql-notice ql-notice-{kind}" '
        f'data-flash data-flash-kind="{kind}"><span>{body}</span></div>'
    )


_here = Path(__file__).parent
templates = Jinja2Templates(directory=str(_here / "templates"))


templates.env.globals["app_version"] = __version__
templates.env.globals["repo_url"] = "https://github.com/jarynclouatre/qobuz-librarian"
templates.env.globals["release_title"] = job_mgr.release_title
# Server epoch at render, so a live elapsed clock can tick from a client-side
# baseline instead of trusting the browser's wall clock against a server epoch.
templates.env.globals["now_ts"] = time.time
# Callable, not a snapshot: the toggle lives in Settings and the downsample
# warnings have to describe whichever mode is active when the page renders.
def _downsample_originals_choice():
    return settings_store.current().get("DOWNSAMPLE_KEEP_ORIGINALS")


templates.env.globals["keeps_ds_originals"] = (
    lambda: _downsample_originals_choice() == "keep"
)
templates.env.globals["ds_originals_chosen"] = (
    lambda: _downsample_originals_choice() in ("keep", "delete")
)
templates.env.globals["backup_retention_days"] = cfg.UPGRADE_BACKUP_RETENTION_DAYS


def _recovery_on_disk(recovery) -> bool:
    """Whether a Repair job's kept-originals folder is still where its record
    says. Drives the job page's pointer honesty: Settings → Diagnostics only
    lists folders it can see, so a job must not send the user there for one
    that is gone. Only a folder whose PARENT is present but which itself
    isn't counts as gone: an unmounted volume makes the whole tree
    disappear without any OSError, and that must read as "can't tell", not
    as licence to clear the alarm."""
    try:
        p = Path(str((recovery or {}).get("location") or ""))
        if recovery.get("kind") == "migration":
            # The kept file can sit in a private folder removed with it, so
            # the album folder is what shows the library is still mounted.
            anchor = str(recovery.get("album_dir") or "")
            return p.exists() or not (
                p.parent.is_dir() or (anchor and Path(anchor).is_dir()))
        if p.is_dir():
            return True
        return not p.parent.is_dir()
    except OSError:
        return True


def _recovery_missing(recovery) -> bool:
    """Whether an exact recovery folder is gone under a mounted parent."""
    return not _recovery_on_disk(recovery)


templates.env.globals["recovery_on_disk"] = _recovery_on_disk


def _retire_gone_recoveries(rows: list[dict]) -> list[dict]:
    """Drop the History recoveries whose kept folders are confirmed gone.

    The job page checks the disk; History only read the record, so a backup
    restored or cleaned up elsewhere stayed pinned to page one under a red
    chip, pointing at a Diagnostics panel with nothing in it. Retiring the
    record where History reads it settles both screens, and the nav's
    attention count with them, without a press per stale scan."""
    kept = []
    for row in rows:
        if (
            row.get("attention") == "recovery"
            and row.get("recoveries")
            and not any(_recovery_on_disk(r) for r in row["recoveries"])
        ):
            job = (job_mgr.registry.get(row["id"])
                   or job_mgr.load_historical_job(row["id"]))
            if job is not None and job_persistence.acknowledge_missing_recoveries(
                job, _recovery_missing
            ):
                continue
        kept.append(row)
    return kept


def _fmt_clock(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""


def _fmt_elapsed(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _quality_shortfall_view(record):
    if not isinstance(record, dict) or record.get("version") != 1:
        return {}

    def label(value):
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return ""
        bits, rate = value
        if (
            type(bits) not in (int, float)
            or type(rate) not in (int, float)
            or not math.isfinite(bits)
            or not math.isfinite(rate)
            or bits <= 0
            or rate <= 0
        ):
            return ""
        return f"{bits}-bit / {rate / 1000:g} kHz"

    target = label(record.get("target"))
    if not target:
        return {}
    source = label(record.get("source"))
    served = label(record.get("served"))
    n_below = record.get("n_below") or 0
    n_unknown = record.get("n_unknown") or 0
    affected = []
    if n_below:
        affected.append(
            f"{n_below} {'track was' if n_below == 1 else 'tracks were'} below target"
        )
    if n_unknown:
        affected.append(
            f"{n_unknown} {'track could' if n_unknown == 1 else 'tracks could'} not be measured"
        )
    return {
        "target": target,
        "source": source,
        "served": served,
        "affected": "; ".join(affected),
        "retry": (
            "The automatic highest-source retry still finished below target."
            if record.get("retried")
            else "No automatic retry was available for this download."
        ),
    }


_LOG_POINTER_RE = re.compile(r"\s*[;.]?\s*(see the log|see job log)\.?\s*$",
                             re.IGNORECASE)


def _strip_log_pointer(message, log_lines):
    """Drop a trailing "see the log" from a message when there is no log."""
    if log_lines:
        return message
    return _LOG_POINTER_RE.sub("", message or "").strip() or message


templates.env.globals["fmt_clock"] = _fmt_clock
templates.env.globals["fmt_elapsed"] = _fmt_elapsed
templates.env.globals["quality_shortfall_view"] = _quality_shortfall_view
templates.env.filters["strip_log_pointer"] = _strip_log_pointer
templates.env.globals["auth_active"] = web_auth.auth_active

static_dir = _here / "static"
static_dir.mkdir(exist_ok=True)


def _asset_version() -> str:
    """Cache-bust key derived from every file served under /static.

    The service worker handles that whole tree cache-first, so any changed,
    added, or removed file must rotate its cache. The semantic app_version is
    for display only.
    """
    h = hashlib.sha256()
    for path in sorted(static_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        relative = path.relative_to(static_dir).as_posix().encode("utf-8")
        h.update(relative)
        h.update(b"\0")
        h.update(content)
        h.update(b"\0")
    return h.hexdigest()[:12] or __version__


_ASSET_VERSION = _asset_version()
templates.env.globals["asset_version"] = _ASSET_VERSION


def _lockout_notice(ip, username="", *, after_failure=False) -> str:
    """How long the login throttle still refuses wrong guesses, or "" when it
    doesn't."""
    left = web_auth.login_lockout_remaining(ip, username)
    if left <= 0:
        return ""
    mins = max(1, (left + 59) // 60)
    lead = "" if after_failure else "Too many failed attempts. "
    return (f"{lead}Try again in {mins} minute{'s' if mins != 1 else ''}, or "
            "restart Qobuz Librarian to clear it.")


def _tr(request, name, context, *, status_code=200, review_badge_ack=None):
    """TemplateResponse wrapper for Starlette 1.0+ signature.

    The navbar badge is computed once per full-page render and injected via
    context; partial-fragment renders skip this entirely. A route that already
    fetched the active job list for its own template (`/queue`, the dashboard)
    can pass it as `pending` and the badge derives from that, with no second
    `pending_and_running()` call on the same render.
    """
    if "pending_job_count" not in context:
        active = context.get("pending") or job_mgr.registry.pending_and_running()
        # The badge counts work in flight, not parked reviews; those sit for
        # weeks by design and have their own review-ready dots, so counting
        # them would pin a permanent "1" to the Queue tab.
        in_flight = [j for j in active
                     if j.status != job_mgr.JobStatus.AWAITING_REVIEW]
        context.setdefault("pending_job_count", len(in_flight))
    context.setdefault("cli_mode", runtime._CLI_MODE)
    context.setdefault("lock_unenforceable", runtime._LOCK_UNENFORCEABLE)
    # Every tool page offered its Start button while writes were paused and let
    # the POST bounce the user onto a 503. Refuse at offer time, not submit time.
    context.setdefault("writes_paused", write_gate._web_writes_paused())
    # Terminal mode is one of eight causes, so carry the true one rather than
    # letting each gated control name the same guess. The full notice travels
    # with it: a greyed button explains itself in a title attribute, which a
    # phone never shows, so every page that greys something can say why.
    if context["writes_paused"]:
        paused = write_gate._writes_paused_notice()
        context.setdefault("writes_paused_notice", paused)
        context.setdefault(
            "writes_paused_reason",
            paused["reason"] if paused else "Downloads and scans are paused.",
        )
    # Error/utility renders (e.g. the 404 page) don't name a nav section; an
    # explicit empty page just leaves every nav link inactive instead of
    # relying on Jinja's undefined-is-falsey behaviour.
    context.setdefault("page", "")
    # Standing health the navbar surfaces on every page, not just the dashboard:
    # a rejected token (auth lost mid-session) and a lock held by another
    # instance both stop downloads, and a user on Search/Queue shouldn't only
    # find out when a job fails. Both are cheap module-level flags, no I/O.
    credentials = qobuz_access._credentials_snapshot()
    creds_ok = credentials.configured
    context.setdefault("qobuz_ready", qobuz_access._qobuz_ready())
    context.setdefault("health_qobuz_missing", not creds_ok)
    context.setdefault(
        "health_token_invalid",
        qobuz_access._token_valid_for(credentials) is False,
    )
    context.setdefault("health_lock_busy", bool(runtime._LOCK_BUSY_PID))
    context.setdefault("upgrade_available", runtime._upgrade_available())
    context.setdefault("discover_available", runtime._discover_available())
    if review_badge_ack:
        surface, generation = review_badge_ack
        if (surface in review_badges.SURFACES
                and (surface != "upgrade" or context["upgrade_available"])):
            review_badges.mark_seen(surface, generation)
    badges = review_badges.snapshot()
    if not context["upgrade_available"]:
        badges = dict(badges)
        badges["upgrade"] = False
    if badges.get("upgrade") or badges.get("downsample"):
        # A dot promises candidates are ready. A saved view the generation
        # authority holds stale has none to show, so it must not carry one.
        # Library keeps its dot: its review still renders, with a caveat.
        authority = generation_state.load()
        badges = dict(badges)
        for surface in ("upgrade", "downsample"):
            if badges.get(surface) and not generation_state.output_is_current(
                surface, state=authority
            ):
                badges[surface] = False
    context.setdefault("nav_review_badges", badges)
    attention_count = job_persistence.attention_count()
    context.setdefault(
        "history_attention",
        ({"count": attention_count, "href": "/queue/history?attention=1"}
         if attention_count else None),
    )
    if name in {"job.html", "_job_body.html"}:
        job = context.get("job")
        holder_id = queue_recovery._startup_recovery_web_job_id()
        context.setdefault("recovery_holder_job_id", (
            holder_id
            if (job.kind == "download" and job.attention == "recovery"
                and not job.recoveries and holder_id != job.id)
            else None
        ))
        nav_page, return_href, return_label = job_labels._job_nav_destination(job)
        context.setdefault("job_nav_page", nav_page)
        context.setdefault("job_return_href", return_href)
        context.setdefault("job_return_label", return_label)
        context.setdefault("job_pending_review", new_release_checks._pending_new_release_review(job))
        context.setdefault(
            "downsample_originals_choice",
            (
                _downsample_originals_choice()
                if job.execute_kind == "downsample"
                else None
            ),
        )
    if name in {"job.html", "_job_body.html", "history.html"}:
        context.setdefault("retry_can_queue", write_gate._retry_can_queue)
        context.setdefault(
            "durable_recovery_control",
            queue_recovery._durable_recovery_control(),
        )
    if name in {"job.html", "_job_body.html", "queue.html", "index.html"}:
        context.setdefault(
            "cancel_protected_job_id",
            job_mgr.durable_recovery_job_id(),
        )
    return templates.TemplateResponse(request=request, name=name,
                                      context=context, status_code=status_code)


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


# What a redirect tells the page it lands on. The URL carries a random key and
# the wording stays here, so a link cannot make a page show text the app never
# wrote.
_NOTICE_TTL = 600
_MAX_NOTICES = 256
_notices: dict[str, tuple[float, str]] = {}
_notices_lock = threading.Lock()


def _notice_key(text) -> str:
    """Hold ``text`` for the page a redirect lands on; returns its URL key."""
    now = time.monotonic()
    key = secrets.token_urlsafe(12)
    with _notices_lock:
        for old in [k for k, (until, _) in _notices.items() if until <= now]:
            del _notices[old]
        while len(_notices) >= _MAX_NOTICES:
            del _notices[next(iter(_notices))]
        _notices[key] = (now + _NOTICE_TTL, str(text))
    return key


def _notice_text(key) -> str:
    """The notice held under ``key``, or "" for anything else."""
    with _notices_lock:
        held = _notices.get(str(key or ""))
    if held is None or held[0] <= time.monotonic():
        return ""
    return held[1]


def render_error_page(request, code, title, msg):
    """Render the app's styled error page from routes or middleware.

    A visitor with no session gets the sign-in shell instead: the app shell
    would hand them the full nav and a Log out button with no way back to the
    login form. Every error render goes through here so the choice is made
    once, including the CSRF refusal, which is raised before the auth gate
    runs and so is the one page that can reach a signed-out browser.
    """
    target = web_auth.signed_out_target(request)
    if target:
        return templates.TemplateResponse(
            request=request, name="error_auth.html",
            context={"title": title, "msg": msg, "target": target,
                     "action": ("Set up your login"
                                if target == web_auth.SETUP_PATH
                                else "Back to sign in")},
            status_code=code)
    return _tr(request, "error.html",
               {"code": code, "title": title, "msg": msg}, status_code=code)


# Cover files, in the order the app trusts them. beets writes cover.jpg for
# both sidecar and embed modes, and the rest are what libraries built by other
# tools carry.
_COVER_FILENAMES = ("cover.jpg", "cover.jpeg", "cover.png",
                    "folder.jpg", "folder.jpeg", "folder.png",
                    "front.jpg", "front.jpeg", "front.png")


def _local_album_art(album_dir):
    """The cover file sitting in an album folder, or None.

    Matched without regard to case, because a library built on a case-sensitive
    filesystem is full of Cover.jpg and Folder.jpg.
    """
    try:
        entries = {entry.name.lower(): entry
                   for entry in os.scandir(str(album_dir))
                   if entry.is_file()}
    except OSError:
        return None
    for filename in _COVER_FILENAMES:
        entry = entries.get(filename)
        if entry is not None:
            return Path(entry.path)
    return None


def _review_cover(job, candidate):
    """Qobuz rows use their cover URL; rows on disk use the folder's cover
    file."""
    payload = candidate.get("payload") or {}
    cover = payload.get("cover")
    if cover:
        return str(cover)
    album_dir = payload.get("album_dir")
    cid = candidate.get("cid")
    if album_dir and cid and _local_album_art(album_dir) is not None:
        return f"/jobs/{job.id}/art/{cid}"
    return ""


templates.env.globals["review_cover"] = _review_cover
