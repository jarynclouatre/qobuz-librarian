"""Auth helpers and shared exceptions.

Exceptions live here because everything imports them; having them in a
dedicated module avoids circular imports (client.py imports AuthLost from
here without needing to import anything back).

detect_auth_lost() lives here because it parses rip subprocess output for
auth signals - no API calls, no session needed.
"""
import re
import tomllib
from pathlib import Path

from qobuz_librarian import config
from qobuz_librarian.ui_cli.colors import C, fmt


# ── Exceptions ────────────────────────────────────────────────────────────────
class AuthLost(Exception):    pass
class CatalogMiss(Exception): pass
class Aborted(Exception):     pass
class QobuzError(Exception):  pass


# Qobuz reached its retry ceiling without a usable answer - the network is
# down, the request timed out, or the API is rate-limiting / 5xx-ing.
class QobuzUnavailable(Exception): pass


# A hook the web layer registers in its lifespan so the dashboard's "saved
# token isn't authenticating" banner reflects the most recent API call, not
# just the startup probe.
_auth_state_listeners: list = []


def register_auth_state_listener(cb) -> None:
    """Register a callback ``cb(valid: bool)`` invoked when an API call
    reveals token validity. A False notify fires whenever client.qobuz_get
    raises AuthLost from a 401, so the web UI can drop its cached "green"
    state without re-probing on every page load."""
    _auth_state_listeners.append(cb)


def notify_auth_state(valid: bool) -> None:
    for cb in list(_auth_state_listeners):
        try:
            cb(valid)
        except Exception:
            pass


_RAW_API_BODY_RE = re.compile(r"^((?:HTTP \d+|bad JSON) from [^:]+):\s+.+$", re.DOTALL)


def friendly_qobuz_error(e):
    """Strip the raw API response body from a QobuzError's message.

    `qobuz_get` raises ``QobuzError("HTTP NNN from endpoint: <body>")`` and
    ``QobuzError("bad JSON from endpoint: <decode error>")``; the trailing
    detail is fine for logs but leaks a response body or a raw
    JSONDecodeError into the user-facing UI. This helper keeps the
    status/endpoint prefix and drops everything after the colon.
    """
    msg = str(e)
    m = _RAW_API_BODY_RE.match(msg)
    if m:
        return m.group(1)
    return msg


class NoCredsError(Exception):
    """No usable Qobuz credentials - env var or streamrip config."""


# ── Token loading ─────────────────────────────────────────────────────────────
def load_qobuz_token():
    """Return (user_id, token). Raises NoCredsError if credentials are absent
    or the streamrip config is unreadable/misconfigured.

    Priority order:
      1. QOBUZ_USER_AUTH_TOKEN / QOBUZ_USER_ID env vars.
      2. streamrip config.toml at STREAMRIP_CONFIG.
    """
    if config.QOBUZ_USER_AUTH_TOKEN:
        return config.QOBUZ_USER_ID or "<env-token>", config.QOBUZ_USER_AUTH_TOKEN

    if not config.STREAMRIP_CONFIG.exists():
        raise NoCredsError(
            f"No Qobuz credentials found. Set QOBUZ_USER_AUTH_TOKEN, or "
            f"open the Settings page in the web UI. "
            f"(streamrip config expected at {config.STREAMRIP_CONFIG})")
    try:
        with open(config.STREAMRIP_CONFIG, "rb") as f:
            cfg = tomllib.load(f)
    except Exception as e:
        raise NoCredsError(f"Couldn't parse streamrip config: {e}") from e
    qz = cfg.get("qobuz", {})
    if not qz.get("use_auth_token"):
        raise NoCredsError(
            "use_auth_token=false in streamrip config. Set QOBUZ_USER_AUTH_TOKEN "
            "or update the streamrip config via the Settings page in the web UI.")
    user_id = str(qz.get("email_or_userid", "")).strip()
    token   = str(qz.get("password_or_token", "")).strip()
    if not user_id or not token:
        raise NoCredsError(
            "Qobuz credentials not set. Set QOBUZ_USER_AUTH_TOKEN, or open "
            "the Settings page in the web UI to configure your auth token.")
    return user_id, token


def write_streamrip_creds(user_id, auth_token) -> bool:
    """Write Qobuz creds into the streamrip config at STREAMRIP_CONFIG.

    Returns False if the config dir/file isn't writable (NAS perms) so
    callers can surface a clear message instead of crashing. Parses with
    tomlkit so the seeded default's inline docs/ordering survive the
    round-trip. Atomic (tmp + os.replace) so a kill mid-write can't leave
    a half-written config that breaks auth on next start.

    Single source of truth: the web Settings handler and the env-var
    sync below both go through here, so streamrip always sees the same
    credential shape regardless of how the user provided them.
    """
    import os
    import tempfile

    import tomlkit
    try:
        config.STREAMRIP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    doc = None
    if config.STREAMRIP_CONFIG.exists():
        try:
            doc = tomlkit.parse(config.STREAMRIP_CONFIG.read_text(encoding="utf-8"))
        except Exception:
            doc = None
    if doc is None:
        # Seed from the bundled streamrip-default.toml.
        _pkg_root = Path(__file__).resolve().parents[3]
        for cand in (Path("/app/docker/streamrip-default.toml"),
                     _pkg_root / "docker" / "streamrip-default.toml"):
            if cand.exists():
                try:
                    doc = tomlkit.parse(cand.read_text(encoding="utf-8"))
                    break
                except Exception:
                    doc = None
        if doc is None:
            # No bundled default reachable (e.g. a pipx install).
            doc = tomlkit.document()
            import importlib.metadata as _im
            try:
                _srv = _im.version("streamrip")
            except _im.PackageNotFoundError:
                _srv = None
            doc["misc"] = tomlkit.table()
            if _srv:
                doc["misc"]["version"] = _srv
            doc["database"] = tomlkit.table()
            # Keep streamrip's downloads db off - on it blocks re-downloading
            # any track the user deleted by hand.
            doc["database"]["downloads_enabled"] = False
            doc["database"]["failed_downloads_enabled"] = True
            # downloads_path / failed_downloads_path are set below,
            # deployment-agnostic, for every branch.
    if "qobuz" not in doc:
        doc["qobuz"] = tomlkit.table()
    doc["qobuz"]["email_or_userid"]   = user_id
    doc["qobuz"]["password_or_token"] = auth_token
    doc["qobuz"]["use_auth_token"]    = True
    # streamrip 2.2.0 REQUIRES the `secrets` key to exist (it's a required
    # field on QobuzConfig - deleting it makes the whole config fail to load).
    doc["qobuz"]["app_id"] = ""
    if "secrets" not in doc["qobuz"]:
        doc["qobuz"]["secrets"] = tomlkit.array()
    if "downloads" not in doc:
        doc["downloads"] = tomlkit.table()
    doc["downloads"]["folder"] = str(config.STAGING_DIR)
    # The bundled default hardcodes the container's /config paths; point
    # streamrip's databases at the actual config dir so a non-/config
    # deployment (bare-metal, custom mount) doesn't hit
    # "OperationalError: unable to open database file".
    if "database" not in doc:
        doc["database"] = tomlkit.table()
    doc["database"]["downloads_path"] = str(
        config.STREAMRIP_CONFIG.parent / "downloads.db")
    doc["database"]["failed_downloads_path"] = str(
        config.STREAMRIP_CONFIG.parent / "failed_downloads.db")
    try:
        target = config.STREAMRIP_CONFIG
        fd, tmp = tempfile.mkstemp(dir=str(target.parent),
                                   prefix=".streamrip.", suffix=".tmp")
        fd_owned = False
        try:
            os.fchmod(fd, 0o600)  # holds the account token - keep it owner-only
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fd_owned = True
                f.write(tomlkit.dumps(doc))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
            # Make the rename durable, matching web/auth.py: a power loss right
            # after replace() must not roll back a token the UI already reported
            # as saved. fsync the parent dir so the new directory entry is on disk.
            try:
                dfd = os.open(str(target.parent), os.O_RDONLY)
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
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except OSError:
        return False
    return True


def sync_streamrip_creds_from_env():
    """If creds come from env vars, mirror them into the streamrip config. The
    app authenticates its own Qobuz API calls straight from
    QOBUZ_USER_AUTH_TOKEN, but downloads shell out to the bundled `rip` CLI,
    which only reads its own config file. Without this, the documented env-var
    setup path lets search/validation succeed while every download fails on
    streamrip's interactive "Enter your Qobuz email:" prompt.
    """
    token = config.QOBUZ_USER_AUTH_TOKEN
    if not token:
        return None
    env_user_id = config.QOBUZ_USER_ID or ""
    existing_user_id = ""
    if config.STREAMRIP_CONFIG.exists():
        try:
            with open(config.STREAMRIP_CONFIG, "rb") as f:
                existing = tomllib.load(f)
            qz = existing.get("qobuz", {})
            existing_user_id = str(qz.get("email_or_userid", "") or "")
            if (qz.get("use_auth_token")
                    and str(qz.get("password_or_token", "")) == token
                    and existing_user_id
                    and (not env_user_id or existing_user_id == env_user_id)):
                return None  # already usable and in sync
        except Exception:
            pass  # unparseable/old → fall through and rewrite
    # Env id wins; fall back to whatever the config already had so a
    # token-only .env (QOBUZ_USER_ID unset) doesn't blank a working id.
    user_id = env_user_id or existing_user_id
    if not user_id:
        import logging
        logging.getLogger("qobuz_librarian").warning(fmt(
            C.YELLOW,
            "  ⚠  QOBUZ_USER_AUTH_TOKEN is set but QOBUZ_USER_ID is not - "
            "downloads need both. Set QOBUZ_USER_ID (or save credentials on "
            "the Settings page); `rip` cannot authenticate from the token "
            "alone."))
        return None
    return write_streamrip_creds(user_id, token)


# ── Streamrip config sanity check ─────────────────────────────────────────────
def verify_streamrip_downloads_folder():
    """Warn loudly if streamrip's downloads.folder doesn't match STAGING_DIR."""
    if not config.STREAMRIP_CONFIG.exists():
        return
    try:
        with open(config.STREAMRIP_CONFIG, "rb") as f:
            cfg = tomllib.load(f)
    except Exception:
        return
    sr_dl = (cfg.get("downloads") or {}).get("folder", "")
    if not sr_dl:
        return
    try:
        if Path(sr_dl).expanduser().resolve() != config.STAGING_DIR.resolve():
            import logging
            log = logging.getLogger("qobuz_librarian")
            log.info(fmt(C.YELLOW, f"  ⚠  streamrip downloads.folder = {sr_dl}"))
            log.info(fmt(C.YELLOW, f"     Qobuz Librarian expects:        {config.STAGING_DIR}"))
            log.info(fmt(C.YELLOW,
                "     Files will land elsewhere; cleanup/import will miss them."))
    except OSError:
        pass


# ── Auth-lost detection (rip subprocess output) ───────────────────────────────
def detect_auth_lost(rip_output):
    """Heuristic check on rip's combined stdout/stderr for auth failures.

    'http 401' (with the protocol prefix) avoids matching plain track numbers.
    'user authentication failed' avoids matching unrelated debug noise.
    """
    o = rip_output.lower()
    # These markers are specific enough to be safe anywhere in the output.
    if any(s in o for s in (
            "http 401",
            "user authentication failed",
            "authenticationerror",
            "invalid credentials")):
        return True
    # "unauthorized" is also a real word in album/track titles (e.g. "The
    # Unauthorized Biography of Reinhold Messner"), and streamrip echoes
    # titles in its progress output.
    for line in o.splitlines():
        if "unauthor" in line and any(k in line for k in (
                "error", "exception", "traceback", "401", "403",
                "denied", "fail")):
            return True
    return False


def detect_rate_limited(rip_output):
    """Heuristic: did Qobuz throttle this rip? Only fires on explicit
    rate-limit signals or persistent network-skips; isolated 'retrying'
    lines are normal streamrip behaviour and don't count."""
    o = rip_output.lower()
    return any(s in o for s in (
        "http 429",
        "too many requests",
        "rate limit",
        "ratelimit",
        "persistent error downloading",  # streamrip exhausted its retries
    ))


def detect_disk_full(rip_output):
    """Heuristic: did the rip/import run out of disk space?"""
    o = rip_output.lower()
    return any(s in o for s in (
        "no space left on device",
        "errno 28",
        "oserror: [errno 28]",
        "disk quota exceeded",
        "errno 122",
    ))
