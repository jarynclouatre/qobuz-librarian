"""Qobuz API session and the core ``qobuz_get`` request.

Shared exceptions and auth helpers live in api/auth.py; this module imports
from there and never the reverse, which keeps the dependency acyclic. The
search/lookup helpers built on ``qobuz_get`` live in api/search.py.
"""
import threading
import time
from contextlib import contextmanager

import requests

from qobuz_librarian import config
from qobuz_librarian.api.auth import (
    AuthLost,
    AuthOutcome,
    CredentialChanged,
    DownloaderNotReady,
    QobuzAccess,
    QobuzEntitlementError,
    QobuzError,
    QobuzUnavailable,
    classify_qobuz_status,
    load_qobuz_credentials,
    notify_auth_state,
    qobuz_capability,
    read_qobuz_credentials,
    token_credential_generation,
)
from qobuz_librarian.ui_cli.colors import C, fmt
from qobuz_librarian.ui_cli.logging import log, vlog


# ── Session ───────────────────────────────────────────────────────────────────
def _ua_string() -> str:
    try:
        from importlib.metadata import version as _pkg_version
        return f"qobuz-librarian/{_pkg_version('qobuz-librarian')} (+streamrip-companion)"
    except Exception:
        return "qobuz-librarian (+streamrip-companion)"


# One requests.Session per thread.
_thread_local = threading.local()


def _get_session() -> requests.Session:
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": _ua_string()})
        _thread_local.session = s
    return s

# Retry on transient failures (rate limit + 5xx).
#
# Per-attempt timeout, derived from WEB_FETCH_TIMEOUT rather than a second
# independently-tuned literal.
_REQUEST_TIMEOUT = max(2, int(config.WEB_FETCH_TIMEOUT) - 2)
_RETRY_STATUSES  = (429, 500, 502, 503, 504)
_MAX_ATTEMPTS    = 3


@contextmanager
def request_deadline(seconds: float):
    """Bound the total wall-time qobuz_get (incl. retries/backoff) spends on
    this thread. Set it on the worker thread that actually runs the request
    (e.g. via call_within under run_in_executor), not the event loop. Nests to
    the tighter deadline; an unset deadline means unbounded (full retries)."""
    prev = getattr(_thread_local, "deadline", None)
    new = time.monotonic() + max(0.0, seconds)
    _thread_local.deadline = new if prev is None else min(prev, new)
    try:
        yield
    finally:
        _thread_local.deadline = prev


def call_within(seconds: float, fn, *args, **kwargs):
    """Run fn under a qobuz_get deadline of `seconds`. Intended as the
    run_in_executor target so the deadline lands on the worker thread."""
    with request_deadline(seconds):
        return fn(*args, **kwargs)


def _remaining_budget() -> float | None:
    deadline = getattr(_thread_local, "deadline", None)
    return None if deadline is None else deadline - time.monotonic()


def _attempt_timeout() -> float | None:
    """Per-request timeout, shrunk to whatever the deadline still allows. None
    means the deadline has already passed - don't even start a request. The
    timeout is NOT floored, so a near-spent deadline yields a sub-second timeout
    (the request fails fast) rather than the old 1.0s floor overrunning it."""
    remaining = _remaining_budget()
    if remaining is None:
        return _REQUEST_TIMEOUT
    if remaining <= 0:
        return None
    return min(_REQUEST_TIMEOUT, remaining)


def _retry_delay(attempt: int, suggested: float) -> float | None:
    """How long to wait before the next attempt, or None to stop retrying:
    attempts exhausted, or the deadline can't fit the wait plus a real retry."""
    if attempt >= _MAX_ATTEMPTS:
        return None
    remaining = _remaining_budget()
    if remaining is None:
        return suggested
    if remaining - suggested < 1.0:
        return None
    return suggested


def _retry_after(resp) -> float | None:
    """Parse Retry-After header (seconds form only - Qobuz never sends an HTTP-date).
    Falls back to None when header is missing or malformed."""
    val = resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        v = float(val)
    except ValueError:
        return None
    if v != v:  # NaN ('nan' parses but propagates through the clamp) - reject it
        return None
    return min(max(v, 0.0), 30.0)


def _retry_sleep(seconds: float):
    """Indirection so tests can monkeypatch backoff to a no-op without
    affecting every other `time.sleep` in the codebase."""
    time.sleep(seconds)


# ── Core request ──────────────────────────────────────────────────────────────
def _net_reason(exc):
    """Short, human reason for a requests failure (not the urllib3 dump)."""
    if isinstance(exc, requests.Timeout):
        return "the Qobuz API timed out"
    if isinstance(exc, requests.ConnectionError):
        return "couldn't reach the Qobuz API (network down or blocked?)"
    if isinstance(exc, requests.TooManyRedirects):
        return "too many redirects from the Qobuz API"
    return "a network error reaching the Qobuz API"


def qobuz_get(endpoint, params, token, *, report_auth: bool = True):
    headers = {"X-App-Id": config.QOBUZ_APP_ID, "X-User-Auth-Token": token}
    url = f"{config.QOBUZ_API_BASE}/{endpoint}"
    generation = token_credential_generation(token)
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        timeout = _attempt_timeout()
        if timeout is None:
            raise QobuzUnavailable(
                f"the Qobuz API timed out (while calling {endpoint}) - try again later")
        try:
            r = _get_session().get(url, params=params, headers=headers,
                                   timeout=timeout)
        except requests.RequestException as e:
            wait = _retry_delay(attempt, min(2 ** (attempt - 1), 8))
            if wait is None:
                raise QobuzUnavailable(
                    f"{_net_reason(e)} (while calling {endpoint}) - try again later") from e
            vlog(f"{endpoint}: network error ({e}); retry {attempt}/{_MAX_ATTEMPTS} in {wait}s")
            _retry_sleep(wait)
            continue
        if r.status_code == 401:
            if report_auth:
                notify_auth_state(AuthOutcome.REJECTED, generation=generation)
            raise AuthLost(
                f"401 from Qobuz {endpoint}",
                credential_generation=generation,
            )
        if r.status_code in _RETRY_STATUSES:
            # `is not None`, not truthiness: a server "Retry-After: 0"
            # (retry immediately) is valid and must not fall back to backoff.
            _ra = _retry_after(r)
            wait = _retry_delay(attempt, _ra if _ra is not None else min(2 ** (attempt - 1), 8))
            if wait is None:
                raise QobuzUnavailable(
                    f"Qobuz API kept returning HTTP {r.status_code} after "
                    f"{attempt} attempt(s) (while calling {endpoint}) - "
                    f"rate-limited or a temporary outage; try again later.")
            if r.status_code == 429:
                # Surface rate-limit waits in the shared logger so the web
                # SSE stream shows "rate-limited, waiting Ns" instead of a
                # silent pause that looks like a hang.
                log.info(fmt(C.YELLOW,
                    f"  ⏳ Qobuz rate-limit - waiting {wait:.0f}s "
                    f"(retry {attempt}/{_MAX_ATTEMPTS})"))
            else:
                vlog(f"{endpoint}: HTTP {r.status_code}; retry "
                     f"{attempt}/{_MAX_ATTEMPTS} in {wait:.1f}s")
            _retry_sleep(wait)
            continue
        if r.status_code != 200:
            outcome = classify_qobuz_status(r.status_code)
            if report_auth and outcome is AuthOutcome.ENTITLEMENT:
                notify_auth_state(outcome, generation=generation)
            raise QobuzError(
                f"HTTP {r.status_code} from {endpoint}: {r.text[:200]}",
                status_code=r.status_code,
                auth_outcome=outcome,
            )
        # A 200 means Qobuz accepted the token.
        if report_auth:
            notify_auth_state(AuthOutcome.ACCEPTED, generation=generation)
        try:
            return r.json()
        except ValueError as e:
            # requests raises its own JSONDecodeError (a ValueError subclass,
            # and simplejson's variant when that's installed) - catch the base
            # so a junk body surfaces as a QobuzError, not an opaque traceback.
            raise QobuzError(f"bad JSON from {endpoint}: {e}") from e


def probe_qobuz(token, *, report_auth: bool = True) -> AuthOutcome:
    """Perform the small uncached check shared by every explicit action."""
    try:
        qobuz_get(
            "album/search",
            {"query": "ok", "limit": 1},
            token,
            report_auth=report_auth,
        )
        return AuthOutcome.ACCEPTED
    except AuthLost:
        return AuthOutcome.REJECTED
    except QobuzUnavailable:
        return AuthOutcome.TEMPORARY
    except QobuzError as exc:
        return exc.auth_outcome
    except Exception:
        return AuthOutcome.TEMPORARY


def authorize_qobuz_action(
    access: QobuzAccess,
    *,
    expected_generation: str = "",
    auth_valid: bool | None = None,
):
    """Live-check one remote action and return its bound credential."""
    credentials = load_qobuz_credentials()
    if expected_generation and credentials.generation != expected_generation:
        raise CredentialChanged(
            "Qobuz credentials changed before the action started. Try again."
        )
    # qobuz_capability() is an offer-time UI decision.  An explicit action is
    # stronger evidence: even a credential rejected by an earlier request gets
    # one fresh, uncached check here so a recovered/replaced server-side token
    # is not trapped behind stale process state.
    live_access = (
        QobuzAccess.CATALOGUE_ACTION
        if access is QobuzAccess.DOWNLOAD_ACTION
        else access
    )
    decision = qobuz_capability(live_access, credentials, auth_valid=None)
    if not decision.allowed:
        raise RuntimeError("Qobuz action capability was refused")

    outcome = probe_qobuz(credentials.token)
    if outcome is AuthOutcome.REJECTED:
        raise AuthLost(
            "Qobuz rejected the saved token",
            credential_generation=credentials.generation,
        )
    if outcome is AuthOutcome.ENTITLEMENT:
        raise QobuzEntitlementError(
            "Qobuz accepted the token, but the account cannot perform this action."
        )
    if outcome in {AuthOutcome.TEMPORARY, AuthOutcome.INCONCLUSIVE}:
        raise QobuzUnavailable(
            "Qobuz could not be reached or could not confirm the request. Try again."
        )

    # A token-only setup is sufficient for catalogue work, but not for the
    # downloader.  Check this after the live probe so the error can truthfully
    # say that Qobuz accepted the token.
    if access is QobuzAccess.DOWNLOAD_ACTION \
            and not credentials.downloader_ready:
        raise DownloaderNotReady(
            "Your Qobuz token works, but downloads also need your "
            "Qobuz user ID. Add it in Settings."
        )

    current = read_qobuz_credentials()
    if current.generation != credentials.generation:
        raise CredentialChanged(
            "Qobuz credentials changed while the action was starting. Try again."
        )
    return credentials


_DOWNLOAD_PREFLIGHT_ATTR = "_qobuz_librarian_download_preflight"


def bind_download_preflight(token, preflight):
    """Carry a CLI-only local downloader check with a generation-bound token."""
    if callable(preflight):
        setattr(token, _DOWNLOAD_PREFLIGHT_ATTR, preflight)
    return token


def authorize_bound_download(token, *, prepare: bool = True):
    """Re-admit a bound download and run its local preflight when requested."""
    expected_generation = getattr(token, "credential_generation", "")
    if not expected_generation:
        return token
    preflight = getattr(token, _DOWNLOAD_PREFLIGHT_ATTR, None)
    credentials = authorize_qobuz_action(
        QobuzAccess.DOWNLOAD_ACTION,
        expected_generation=expected_generation,
    )
    if prepare and callable(preflight):
        preflight()
    return bind_download_preflight(credentials.token, preflight)
