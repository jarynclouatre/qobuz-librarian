"""The saved Qobuz credentials, the token check and the bounded Qobuz calls."""
import asyncio
import threading

from qobuz_librarian import config as cfg
from qobuz_librarian import redaction
from qobuz_librarian.api import auth as api_auth
from qobuz_librarian.api import client as api_client
from qobuz_librarian.api.auth import (
    AuthEvidence,
    AuthLost,
    AuthOutcome,
    CredentialChanged,
    DownloaderNotReady,
    NoCredsError,
    QobuzAccess,
    QobuzEntitlementError,
    QobuzUnavailable,
    credentials_from_values,
    qobuz_capability,
)
from qobuz_librarian.web import job_persistence, runtime
from qobuz_librarian.web import jobs as job_mgr

_TOKEN_VALID: bool | None = None


_TOKEN_GENERATION: str | None = None


_AUTH_LOSS_NOTIFIED_GENERATIONS: set[str] = set()


_CREDENTIAL_LOCK = threading.RLock()


def set_token_state(valid: bool | None, generation: str | None) -> None:
    global _TOKEN_VALID, _TOKEN_GENERATION
    _TOKEN_VALID = valid
    _TOKEN_GENERATION = generation


async def _qobuz_call(fn, *args, **kwargs):
    """Run one Qobuz API call off the event loop, held to WEB_FETCH_TIMEOUT."""
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(None, lambda: api_client.call_within(
            cfg.WEB_FETCH_TIMEOUT, fn, *args, **kwargs)),
        timeout=cfg.WEB_FETCH_TIMEOUT)


def _authorize_qobuz_live(access: QobuzAccess, *, expected_generation=""):
    """Run the bounded uncached check used before a Web action is admitted."""
    return api_client.call_within(
        cfg.WEB_TEST_AUTH_TIMEOUT,
        api_client.authorize_qobuz_action,
        access,
        expected_generation=expected_generation,
        auth_valid=_token_valid_for(),
    )


async def _authorize_qobuz_for_web(access: QobuzAccess, *,
                                    expected_generation=""):
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            None,
            lambda: _authorize_qobuz_live(
                access,
                expected_generation=expected_generation,
            ),
        ),
        timeout=cfg.WEB_TEST_AUTH_TIMEOUT,
    )


# What _authorize_qobuz_for_web raises when the action may not start.
_QOBUZ_ACTION_ERRORS = (
    NoCredsError,
    AuthLost,
    QobuzUnavailable,
    QobuzEntitlementError,
    DownloaderNotReady,
    CredentialChanged,
    asyncio.TimeoutError,
)


def _credential_generation_is_active(generation: str) -> bool:
    return bool(generation) and api_auth.read_qobuz_credentials().generation == generation


def _scrub_stored_credentials(logger) -> None:
    """One pass over everything written before the masking existed. A Qobuz
    error names the URL it called, and that URL carries the account email and
    the auth token, so stored job records and the app's own log files can still
    hold a working credential after an upgrade. Runs before the log handler is
    attached, so rewriting a log file cannot cut a handler off from it, and the
    marker keeps it to one pass: nothing written afterwards can carry a secret.
    """
    marker = cfg.DATA_DIR / ".credential_scrub"
    try:
        if marker.exists():
            return
    except OSError:
        return
    complete = True
    try:
        # Reading them registers the live values, so a token logged with no
        # parameter name beside it is masked too.
        api_auth.read_qobuz_credentials()
    except Exception:
        complete = False
    rows = 0
    try:
        job_persistence.init()
        scrubbed = job_persistence.scrub_stored_secrets()
        if scrubbed is None:
            complete = False
        else:
            rows = scrubbed
    except Exception:
        complete = False
    files = 0
    try:
        targets = list(cfg.DATA_DIR.glob("qobuz-librarian*.log*"))
    except OSError:
        complete = False
        targets = []
    targets.append(cfg.FETCH_LOG_FILE)
    for path in targets:
        try:
            scrubbed = redaction.scrub_file(path)
            if scrubbed is None:
                complete = False
            elif scrubbed:
                files += 1
        except Exception:
            complete = False
    if rows or files:
        logger.info(
            f"Masked account details in {rows} stored job record(s) and "
            f"{files} log file(s) written by an earlier version.")
    if complete:
        try:
            marker.touch()
        except OSError:
            pass
    else:
        logger.warning(
            "Stored credential cleanup was incomplete and will retry next start.")


def _classify_token(token):
    """Test a token without publishing evidence for an unsaved credential."""
    return api_client.probe_qobuz(token, report_auth=False)


def _on_auth_state(evidence: AuthEvidence) -> None:
    """Apply evidence only when it belongs to the active saved credential."""
    global _TOKEN_GENERATION, _TOKEN_VALID
    if evidence.outcome not in {
        AuthOutcome.ACCEPTED,
        AuthOutcome.REJECTED,
        AuthOutcome.ENTITLEMENT,
    }:
        return
    with runtime._auto_check_lock:
        if runtime._SHUTTING_DOWN:
            return
        credentials = _credentials_snapshot()
        if not credentials.configured \
                or evidence.generation != credentials.generation:
            return
        valid = evidence.outcome in {
            AuthOutcome.ACCEPTED,
            AuthOutcome.ENTITLEMENT,
        }
        _TOKEN_VALID = valid
        _TOKEN_GENERATION = evidence.generation
        if (not valid and evidence.generation
                not in _AUTH_LOSS_NOTIFIED_GENERATIONS):
            _AUTH_LOSS_NOTIFIED_GENERATIONS.add(evidence.generation)
            job_mgr.fire_auth_lost_hook()


def _token_valid_for(credentials=None) -> bool | None:
    credentials = credentials or _credentials_snapshot()
    if not credentials.configured:
        return None
    if _TOKEN_GENERATION is not None \
            and _TOKEN_GENERATION != credentials.generation:
        return None
    return _TOKEN_VALID


def _qobuz_access(access: QobuzAccess):
    credentials = _credentials_snapshot()
    return qobuz_capability(
        access,
        credentials,
        auth_valid=_token_valid_for(credentials),
    )


def _qobuz_ready() -> bool:
    """True when Qobuz-dependent UI actions are worth offering."""
    return _qobuz_access(QobuzAccess.CATALOGUE_ACTION).allowed


async def _probe_token():
    """One-shot startup check that the saved token still authenticates.

    Sets ``_TOKEN_VALID`` to True/False/None: None means the result is
    inconclusive (no token saved, or the probe couldn't reach Qobuz), so
    the dashboard treats it as "don't nag yet."
    """
    credentials = _credentials_snapshot()
    if not credentials.configured:
        return
    try:
        verdict = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None, lambda: api_client.call_within(cfg.WEB_TEST_AUTH_TIMEOUT,
                                          _classify_token,
                                          credentials.token)),
            timeout=cfg.WEB_TEST_AUTH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        verdict = AuthOutcome.TEMPORARY
    if verdict in {
        AuthOutcome.ACCEPTED,
        AuthOutcome.REJECTED,
        AuthOutcome.ENTITLEMENT,
    }:
        _on_auth_state(AuthEvidence(credentials.generation, verdict))


def _get_token():
    return api_auth.load_qobuz_token()[1]


def _get_optional_token():
    if not _creds_ok():
        return None
    try:
        return _get_token()
    except Exception:
        return None


def _read_creds():
    credentials = api_auth.read_qobuz_credentials()
    if not credentials.configured:
        return {}
    return {
        "user_id": credentials.user_id,
        "auth_token": credentials.token,
        "_generation": credentials.generation,
        "_source": credentials.source,
    }


def _creds_ok() -> bool:
    return bool(_read_creds().get("auth_token"))


def _credentials_snapshot():
    values = _read_creds()
    return credentials_from_values(
        values.get("user_id", ""),
        values.get("auth_token", ""),
        source=values.get("_source", "web"),
    )


def _write_creds(user_id, auth_token) -> bool:
    """Write credentials into the streamrip config."""
    return api_auth.write_streamrip_creds(user_id, auth_token)
