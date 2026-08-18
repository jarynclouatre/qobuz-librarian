"""Optional single-user login for the web UI.

One username + password, opt-out via WEB_AUTH=none. Follows web/csrf.py's
cookie conventions (HttpOnly/SameSite, secrets.compare_digest) and persists
the credential the way the streamrip token is persisted: an atomic 0600
file in DATA_DIR.

The session cookie carries a random per-login token, not the credential secret.
A digest bound to the current credential generation is persisted to disk (an
atomic 0600 file in DATA_DIR) with an expiry and reloaded on restart, so an
ordinary restart does not sign browsers out while a password rotation cannot
revive stale sessions; a session also ends on logout or expiry.
"""
import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
import time
import unicodedata
import urllib.parse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse, Response

from qobuz_librarian import config as cfg

log = logging.getLogger("qobuz_librarian")

SESSION_COOKIE = "ql_session"
LOGIN_PATH = "/login"
SETUP_PATH = "/setup"
MIN_PASSWORD_LEN = 15


class PasswordRejected(ValueError):
    pass


class CredentialSeedError(RuntimeError):
    pass


class SessionPersistenceError(RuntimeError):
    pass


_BLOCKED_PASSWORD_KEYS = frozenset({
    "123456789012345",
    "aaaaaaaaaaaaaaa",
    "adminadminadmin",
    "changeme123456",
    "changemechangeme",
    "changemetosomethingstrong",
    "correcthorsebatterystaple",
    "iloveyouiloveyou",
    "letmeinletmein",
    "password123456",
    "passwordpassword",
    "qobuzlibrarian",
    "qobuzlibrarian123",
    "qobuzlibrarianadmin",
    "qobuzlibrarianpassword",
    "qwerty123456789",
    "welcomewelcome",
    "xxxxxxxxxxxxxxx",
})

# Reachable without a session: the auth pages handle their own gating, the
# health probes must answer monitors, and the sign-in and setup pages register
# the service worker and pull its static assets before anyone has signed in.
_OPEN_PATHS = {"/healthz", "/readyz", "/sw.js", "/favicon.ico"}
_OPEN_PREFIXES = ("/static/",)

_PBKDF2_ROUNDS = 600_000
_COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days, matching the CSRF cookie


def safe_next_path(raw) -> str:
    """A local path it's safe to send the browser back to after login, or "".

    Only same-app paths pass: a leading "/" with a normal second character.
    "//host" (protocol-relative) and "/\\host" (browsers fold backslash into
    slash) would leave the app. An attacker could mail a login link that lands
    on a look-alike site after a *successful* sign-in. Control characters are
    header-injection material, and bouncing back to /login or /setup would
    just loop.
    """
    p = str(raw or "").strip()
    if not p.startswith("/") or len(p) > 512:
        return ""
    if p.startswith(("//", "/\\")) or "\\" in p:
        return ""
    if any(ord(ch) < 0x20 or ch == "\x7f" for ch in p):
        return ""
    bare = p.split("?", 1)[0]
    if bare in (LOGIN_PATH, SETUP_PATH) or bare.startswith("/api/"):
        return ""
    return p


def safe_current_path(raw, host: str) -> str:
    """Local path from an HTMX current-page URL, or an empty string."""
    value = str(raw or "").strip()
    if value.startswith("/"):
        return safe_next_path(value)
    try:
        current = urllib.parse.urlsplit(value)
    except ValueError:
        return ""
    if (current.scheme not in ("http", "https") or not current.netloc
            or current.username or current.password
            or current.netloc.casefold() != (host or "").casefold()):
        return ""
    target = current.path or "/"
    if current.query:
        target += "?" + current.query
    return safe_next_path(target)

# Credential file cache.
_cred_cache: dict | None = None
_cred_cache_path: str | None = None

# Login failure tracking: IP → list of failure timestamps.
_login_failures: dict[str, list[float]] = {}
_login_lock = threading.Lock()
_LOGIN_MAX = 5
_LOGIN_WINDOW = 3600
# Failures are counted over _LOGIN_WINDOW, but the door reopens _LOGIN_LOCKOUT
# after the last one rather than after the oldest ages out. A typo run costs the
# owner fifteen minutes instead of an hour, while an attacker who keeps guessing
# keeps pushing the lockout forward: five tries, then one every fifteen minutes.
_LOGIN_LOCKOUT = 900
# Backstop against a flood of distinct (or spoofed) source IPs filling the
# table; stale buckets are pruned continuously, this caps the live set.
_MAX_TRACKED_IPS = 2048

# Per-username failure tracking, in ADDITION to per-IP: an attacker on a /64 of
# residential IPv6 can rotate source addresses to dodge the per-IP throttle, so
# also lock the targeted account after _USER_LOGIN_MAX failures regardless of
# source IP.
_user_failures: dict[str, list[float]] = {}
_USER_LOGIN_MAX = 10
_MAX_TRACKED_USERS = 1024

# Active session tokens: a credential-generation-bound digest of the random
# per-login cookie value → expiry epoch seconds. Binding the lookup to the
# current session secret makes a stale sessions file harmless after a password
# rotation, even if clearing that second file failed after credential publish.
_SESSIONS_FILE = cfg.DATA_DIR / ".qobuz_web_sessions.json"
_sessions_lock = threading.Lock()


def _token_digest(token: str) -> str:
    # _read is defined below but resolved only when this function is called,
    # after module initialisation. An unavailable/unconfigured credential file
    # contributes an empty generation and still fails closed at the auth gate.
    generation = str(_read().get("session_secret") or "")
    material = generation.encode("utf-8") + b"\0" + token.encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _load_sessions() -> dict[str, float]:
    try:
        raw = json.loads(_SESSIONS_FILE.read_text(encoding="utf-8"))
        now = time.time()
        return {str(k): float(v) for k, v in raw.items() if float(v) > now}
    except (OSError, ValueError, AttributeError):
        return {}


def _save_sessions_locked() -> bool:
    fd = None
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(dir=str(cfg.DATA_DIR),
                                   prefix=".qobuz_web_sessions.", suffix=".tmp")
        stream = os.fdopen(fd, "w", encoding="utf-8")
        fd = None
        with stream as f:
            json.dump(_sessions, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, _SESSIONS_FILE)
        tmp = ""
        return True
    except OSError:
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


_sessions: dict[str, float] = _load_sessions()


def auth_disabled() -> bool:
    """True only when WEB_AUTH is the literal 'none'. Blank/unset leaves auth
    on. Disabling is a deliberate opt-out, never the side effect of an empty
    field. Read live from the env so it tracks the running environment."""
    return os.environ.get("WEB_AUTH", "").strip().lower() == "none"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                             _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def _password_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(char for char in normalized if char.isalnum())


def new_password_error(username: str, password: str) -> str:
    # Measure the trimmed password so spaces cannot pad a short one up to the
    # minimum, and so a password of nothing but spaces is refused outright.
    if len(password.strip()) < MIN_PASSWORD_LEN:
        return (f"Use a password of at least {MIN_PASSWORD_LEN} characters, "
                "not counting spaces at the ends.")
    password_key = _password_key(password)
    username_key = _password_key(username)
    username_variants = ({username_key, username_key * 2, username_key * 3}
                         if username_key else set())
    if password_key in _BLOCKED_PASSWORD_KEYS or password_key in username_variants:
        return ("Choose a less common password. A long passphrase of unrelated "
                "words works well.")
    return ""


def _verify_hash(stored: str, password: str) -> bool:
    try:
        algo, rounds, salt_hex, want_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                  bytes.fromhex(salt_hex), int(rounds))
    except (ValueError, AttributeError):
        return False
    return secrets.compare_digest(got.hex(), want_hex)


def _constant_time_eq(a: str, b: str) -> bool:
    """compare_digest, but on UTF-8 bytes so a non-ASCII value (a unicode
    username, or a junk cookie a client can send as raw latin-1) compares
    cleanly instead of raising the TypeError compare_digest gives for
    non-ASCII strings."""
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _read() -> dict:
    global _cred_cache, _cred_cache_path
    current = str(cfg.WEB_AUTH_FILE)
    if _cred_cache is not None and _cred_cache_path == current:
        return _cred_cache
    try:
        data = json.loads(cfg.WEB_AUTH_FILE.read_text(encoding="utf-8"))
        _cred_cache = data if isinstance(data, dict) else {}
        _cred_cache_path = current
        return _cred_cache
    except FileNotFoundError:
        # No creds file yet (fresh install), a stable "unconfigured" state,
        # safe to cache so the open-setup phase doesn't re-stat every request.
        _cred_cache = {}
        _cred_cache_path = current
        return _cred_cache
    except (OSError, ValueError):
        # A transient read failure (NFS/CIFS not ready, a brief I/O error, a
        # half-written file) must NOT be cached: caching {} would permanently
        # report "no creds configured" and re-expose the open /setup page until
        # the next set_credentials(). Return a throwaway dict and retry next call.
        _cred_cache = None
        _cred_cache_path = None
        return {}


def credentials_configured() -> bool:
    d = _read()
    return bool(d.get("username") and d.get("password_hash")
                and d.get("session_secret"))


def creds_file_present_but_unreadable() -> bool:
    """True when the creds file exists but can't be read as valid credentials,
    a transient I/O error or a corrupt/half-written file. Distinct from a fresh
    install (no file at all): something IS configured here, we just can't read it,
    so callers must fail closed rather than fall back to the unauthenticated
    /setup page, which would overwrite the admin account."""
    try:
        present = cfg.WEB_AUTH_FILE.exists()
    except OSError:
        return True  # can't even stat the volume → treat as present-but-unavailable
    return present and not credentials_configured()


def set_credentials(username: str, password: str) -> bool:
    """Persist username + password hash + a fresh session secret, atomically
    and 0600. Returns False if the data volume isn't writable so callers can
    show a clear message instead of 500ing. The new session secret rotates on
    every call, so resetting the password logs out any existing browser. Raises
    PasswordRejected before writing when the new password does not meet policy."""
    global _cred_cache, _cred_cache_path
    error = new_password_error(username, password)
    if error:
        raise PasswordRejected(error)
    # Never overwrite an existing-but-unreadable creds file: a transient read
    # error must not let the open /setup page clobber the admin account.
    if creds_file_present_but_unreadable():
        return False
    _cred_cache = None
    _cred_cache_path = None
    payload = {
        "username": username,
        "password_hash": hash_password(password),
        "session_secret": secrets.token_urlsafe(32),
    }
    try:
        cfg.WEB_AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(cfg.WEB_AUTH_FILE.parent),
                                   prefix=".qobuz_web_auth.", suffix=".tmp")
        fd_owned = False
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fd_owned = True
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, cfg.WEB_AUTH_FILE)
            # Make the rename durable: a power loss right after replace() must not
            # revert to "no creds" and re-open the unauthenticated /setup page on
            # reboot. fsync the parent dir so the new directory entry is on disk.
            try:
                dfd = os.open(str(cfg.WEB_AUTH_FILE.parent), os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
        finally:
            if not fd_owned:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError:
        return False
    # A password change (or first setup) invalidates every existing session, so
    # tokens minted under the prior password stop authenticating.
    revoke_all_sessions()
    return True


def _env_password() -> str:
    """WEB_AUTH_PASSWORD from the env, or WEB_AUTH_PASSWORD_FILE (Docker-secret
    form) when the env var is unset, so the admin password can stay out of
    `docker inspect` and the process environment. This matches
    QOBUZ_USER_AUTH_TOKEN_FILE for the Qobuz token."""
    pw = os.environ.get("WEB_AUTH_PASSWORD", "")
    if pw:
        return pw
    path = os.environ.get("WEB_AUTH_PASSWORD_FILE", "").strip()
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            # Strip only the trailing newline a file editor adds.
            password = f.read().rstrip("\n")
    except OSError as exc:
        raise CredentialSeedError(
            "WEB_AUTH_PASSWORD_FILE could not be read"
        ) from exc
    if not password:
        raise CredentialSeedError("WEB_AUTH_PASSWORD_FILE is empty")
    return password


def apply_env_credentials() -> str:
    """Seed the web login from WEB_AUTH_USER / WEB_AUTH_PASSWORD so a deployment
    comes up already locked down instead of exposing the open setup screen to
    whoever reaches it first, and so the password can be reset by editing the
    environment and restarting.

    The env values win when present: a changed password re-seeds (rotating the
    session secret, which logs existing browsers out, as a password change
    should); an unchanged one is left alone so a plain restart doesn't churn the
    secret. Returns a status for the caller to log: 'noop', 'partial',
    'applied', 'unchanged', or 'failed'.
    """
    if auth_disabled():
        return "noop"
    user = os.environ.get("WEB_AUTH_USER", "").strip()
    password = _env_password()
    if not user and not password:
        return "noop"
    if not user or not password:
        return "partial"
    d = _read()
    if (d.get("password_hash")
            and _constant_time_eq(user, d.get("username") or "")
            and _verify_hash(d.get("password_hash"), password)):
        return "unchanged"
    if not set_credentials(user, password):
        return "failed"
    return "applied"


def verify_login(username: str, password: str) -> bool:
    """Constant-time check of both fields. The password is always run through
    the KDF when a hash exists, so a wrong username and a wrong password take
    the same time and neither is distinguishable by timing."""
    d = _read()
    stored_hash = d.get("password_hash") or ""
    if not stored_hash:
        return False
    user_ok = _constant_time_eq(username, d.get("username") or "")
    pass_ok = _verify_hash(stored_hash, password)
    return user_ok and pass_ok


def mint_session() -> str:
    """Issue a fresh per-login session token (the cookie value) and return it."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    digest = _token_digest(token)
    with _sessions_lock:
        for t, exp in list(_sessions.items()):
            if exp <= now:
                del _sessions[t]
        previous = _sessions.get(digest)
        _sessions[digest] = now + _COOKIE_MAX_AGE
        if not _save_sessions_locked():
            if previous is None:
                _sessions.pop(digest, None)
            else:
                _sessions[digest] = previous
            raise SessionPersistenceError("The web session could not be saved.")
    return token


def revoke_session(token: str) -> bool:
    """Durably invalidate one session (log a single browser out).

    Returns False without changing the live session when its durable removal
    cannot be published, so the caller can report that logout did not happen
    and let the authenticated browser retry.
    """
    if not token:
        return True
    with _sessions_lock:
        digest = _token_digest(token)
        previous = _sessions.pop(digest, None)
        if previous is None:
            return True
        if not _save_sessions_locked():
            _sessions[digest] = previous
            return False
        return True


def revoke_all_sessions() -> None:
    """Invalidate every session (e.g. on a password change)."""
    with _sessions_lock:
        _sessions.clear()
        _save_sessions_locked()


def verify_session(cookie_value: str) -> bool:
    if not cookie_value:
        return False
    now = time.time()
    with _sessions_lock:
        digest = _token_digest(cookie_value)
        exp = _sessions.get(digest)
        if exp is None:
            return False
        if exp <= now:
            del _sessions[digest]
            _save_sessions_locked()
            return False
        return True


def _secure(request) -> bool:
    return (request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto") == "https")


def set_session_cookie(response, request) -> None:
    # SameSite=strict (matching the CSRF cookie): the session is the auth
    # credential, and no app flow needs it carried on a cross-site first hop.
    # a deep link from elsewhere just bounces once through /login, which
    # re-issues it. Strict keeps the auth cookie off every cross-site request.
    response.set_cookie(
        SESSION_COOKIE,
        mint_session(),
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=_secure(request),
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE, samesite="strict")


def auth_active() -> bool:
    """Auth is both enabled and set up, the only state in which a Log out
    control makes sense. Exposed to templates as a global."""
    return not auth_disabled() and credentials_configured()


def signed_out_target(request) -> str:
    """Where an error page should send this visitor when it must not show them
    the app shell, or "" when the shell is theirs to see.

    Mirrors AuthMiddleware's own decision. It has to live outside the
    middleware because the CSRF refusal is raised before the gate runs: a
    tokenless POST to the sign-in form was answering with the whole nav, a
    Settings link and a Log out button, and no route back to signing in.
    """
    if auth_disabled():
        return ""
    if not credentials_configured():
        return SETUP_PATH
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie and verify_session(cookie):
        return ""
    return LOGIN_PATH


def _prune_failures(now: float) -> None:
    """Drop buckets with no in-window failures left. Caller holds _login_lock."""
    for bucket in (_login_failures, _user_failures):
        stale = [k for k, ts in bucket.items()
                 if not any(now - t < _LOGIN_WINDOW for t in ts)]
        for k in stale:
            del bucket[k]


def _norm_user(username: str) -> str:
    return (username or "").strip().casefold()


_warned_untrusted_proxy = False


def client_ip(request) -> str:
    """The address the login throttle counts against.

    uvicorn resolves this from X-Forwarded-For already, but only for proxies
    FORWARDED_ALLOW_IPS names, and its default (127.0.0.1) never matches a
    proxy on a Docker network. Left unset, every visitor arrives as the proxy
    and the per-address throttle becomes one bucket for the whole deployment,
    so a stranger's wrong guesses lock the owner out too. Say so once, naming
    the address to trust, rather than throttling everyone as one in silence.
    """
    global _warned_untrusted_proxy
    peer = (request.client.host if request.client else "") or ""
    chain = {p.strip() for p in
             request.headers.get("x-forwarded-for", "").split(",") if p.strip()}
    # A resolved address is one of the forwarded entries; the raw peer is not,
    # because a proxy appends the client it saw rather than itself.
    if chain and peer and peer not in chain and not _warned_untrusted_proxy:
        _warned_untrusted_proxy = True
        log.warning(
            "Sign-ins are arriving through a proxy at %s whose forwarded "
            "address isn't trusted, so the failed-login limit counts every "
            "visitor as one and a stranger's wrong guesses can lock you out. "
            "Set FORWARDED_ALLOW_IPS=%s in .env and restart.", peer, peer)
    return peer or "unknown"


def _locked(times: list[float], limit: int, now: float) -> bool:
    """Whether these in-window failures still hold the door shut."""
    return len(times) >= limit and now - max(times) < _LOGIN_LOCKOUT


# A locked bucket still gets its password checked: someone who already knows it
# was never stopped by a wait, and behind a proxy the wait can be triggered by a
# stranger, leaving the owner with a container restart as the only way back in.
# What a flood would be spending is the 600k round KDF, so only a couple of
# those checks run at once and the rest are refused rather than queued. That
# caps the CPU an attacker can take however many connections they open, while a
# person retrying one attempt at a time never meets it.
_LOCKED_CHECKS = threading.BoundedSemaphore(2)


def take_locked_check_slot() -> bool:
    """Claim one of the concurrent password checks allowed while locked."""
    return _LOCKED_CHECKS.acquire(blocking=False)


def release_locked_check_slot() -> None:
    _LOCKED_CHECKS.release()


def check_login_rate_limit(ip: str, username: str = "") -> bool:
    """True if BOTH this IP and this account may attempt a login. The per-account
    counter (keyed on the submitted username) blocks an attacker who rotates
    source IPs against one account; the per-IP counter alone is bypassable."""
    now = time.monotonic()
    uname = _norm_user(username)
    with _login_lock:
        times = [t for t in _login_failures.get(ip, []) if now - t < _LOGIN_WINDOW]
        if times:
            _login_failures[ip] = times
        else:
            _login_failures.pop(ip, None)
        if _locked(times, _LOGIN_MAX, now):
            return False
        if uname:
            utimes = [t for t in _user_failures.get(uname, [])
                      if now - t < _LOGIN_WINDOW]
            if utimes:
                _user_failures[uname] = utimes
            else:
                _user_failures.pop(uname, None)
            if _locked(utimes, _USER_LOGIN_MAX, now):
                return False
        return True


def login_lockout_remaining(ip: str, username: str = "") -> int:
    """Seconds until this IP or account may try again; 0 when not locked.

    The throttle is deliberately checked before the password is verified, so a
    correct password cannot clear it. That is what stops an attacker learning
    they have found it. The user therefore has to be told how long it is, and
    the message that says "wait an hour" is wrong for all but the first second.
    """
    now = time.monotonic()
    uname = _norm_user(username)
    with _login_lock:
        waits = []
        times = [t for t in _login_failures.get(ip, []) if now - t < _LOGIN_WINDOW]
        if _locked(times, _LOGIN_MAX, now):
            waits.append(_LOGIN_LOCKOUT - (now - max(times)))
        if uname:
            utimes = [t for t in _user_failures.get(uname, [])
                      if now - t < _LOGIN_WINDOW]
            if _locked(utimes, _USER_LOGIN_MAX, now):
                waits.append(_LOGIN_LOCKOUT - (now - max(utimes)))
    return int(max(0, max(waits))) if waits else 0


def record_login_failure(ip: str, username: str = "") -> None:
    now = time.monotonic()
    uname = _norm_user(username)
    with _login_lock:
        _prune_failures(now)
        if ip not in _login_failures and len(_login_failures) >= _MAX_TRACKED_IPS:
            del _login_failures[min(_login_failures,
                                    key=lambda k: min(_login_failures[k]))]
        _login_failures.setdefault(ip, []).append(now)
        if uname:
            if (uname not in _user_failures
                    and len(_user_failures) >= _MAX_TRACKED_USERS):
                del _user_failures[min(_user_failures,
                                       key=lambda k: min(_user_failures[k]))]
            _user_failures.setdefault(uname, []).append(now)


def clear_login_failures(ip: str, username: str = "") -> None:
    """Forget an IP's (and the account's) failures after a successful login so an
    earlier typo run doesn't leave the next session one slip from a lockout."""
    uname = _norm_user(username)
    with _login_lock:
        _login_failures.pop(ip, None)
        if uname:
            _user_failures.pop(uname, None)


class AuthMiddleware(BaseHTTPMiddleware):
    """Gate every route behind a session cookie once a login is configured.

    Sits inside the CSRF middleware so the login/setup POSTs still get CSRF
    validation and the redirects it returns still pick up the CSRF cookie and
    security headers on the way out.
    """

    async def dispatch(self, request, call_next):
        if auth_disabled():
            return await call_next(request)
        # Decide on the raw ASGI path, not request.url.path: Starlette rebuilds
        # request.url from the client-supplied Host header, so a malformed Host
        # ("example.com/login?x=") can make url.path read "/login" and turn a
        # protected route into an open one (CVE-2026-48710). scope["path"] is the
        # real routed path and is immune to Host-header confusion.
        path = request.scope["path"]
        if path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES):
            return await call_next(request)

        creds = _read()
        configured = bool(creds.get("username") and creds.get("password_hash")
                          and creds.get("session_secret"))
        if not configured:
            if creds_file_present_but_unreadable():
                # The creds file is there but unreadable (transient I/O or a
                # corrupt/half-written file).
                return Response(
                    "Login is configured but its credentials can't be read right "
                    "now. Try again shortly.", status_code=503)
            # Nothing protects the box yet. Force the setup screen, but let
            # the setup GET/POST through so a login can actually be created.
            if path == SETUP_PATH:
                return await call_next(request)
            return self._reject(request, SETUP_PATH)

        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie and verify_session(cookie):
            return await call_next(request)
        if path in (LOGIN_PATH, SETUP_PATH):
            return await call_next(request)
        return self._reject(request, LOGIN_PATH)

    @staticmethod
    def _reject(request, location):
        if request.headers.get("HX-Request") == "true":
            target = safe_current_path(
                request.headers.get("HX-Current-URL"),
                request.headers.get("host", ""),
            )
            if location == LOGIN_PATH and target:
                location += "?next=" + urllib.parse.quote(target)
            return Response(status_code=401, headers={"HX-Redirect": location})
        # API/SSE callers get a machine-readable 401.
        if request.scope["path"].startswith("/api/"):
            return JSONResponse({"detail": "authentication required"},
                                status_code=401)
        # Remember where a plain page GET was headed so login can land there
        # instead of dumping every deep link on the dashboard.
        if location == LOGIN_PATH and request.method == "GET":
            target = request.scope["path"]
            qs = request.scope.get("query_string", b"").decode("utf-8", "ignore")
            if qs:
                target += "?" + qs
            if target != "/" and safe_next_path(target):
                location = LOGIN_PATH + "?next=" + urllib.parse.quote(target)
        return RedirectResponse(url=location, status_code=303)
